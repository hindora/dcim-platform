"""Alarm orchestration: rules in, alarms out.

Five sources produce alarms - metric thresholds, SNMP traps, Redfish events,
BACnet COV and communication failure - and they all funnel through ONE lifecycle
here. Letting each source grow its own half-correct raise/clear handling is how
alarm systems end up with conditions that can be raised but never cleared.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.alarms import correlation, staleness
from app.alarms.engine import (
    AlarmKey,
    Candidate,
    ClearSignal,
    DwellState,
    Rule,
    evaluate,
)
from app.core.logging import get_logger
from app.repositories import alarms as repo

log = get_logger("alarms")

DWELL_HASH = "dcim:dwell"
DWELL_TTL_S = 24 * 3600
RULE_REFRESH_S = 60.0


@dataclass(slots=True)
class AlarmAction:
    kind: str                 # alarm_created | alarm_updated | alarm_cleared
    alarm: dict[str, Any]


class AlarmService:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._rules: list[Rule] = []
        self._by_metric: dict[str, list[Rule]] = {}
        self._loaded_at = 0.0

    # ------------------------------------------------------------- rules

    async def ensure_rules(self, session: AsyncSession, force: bool = False) -> None:
        if not force and (time.monotonic() - self._loaded_at) < RULE_REFRESH_S:
            return
        rows = await repo.load_rules(session)
        rules = [
            Rule(
                id=r["id"], name=r["name"], alarm_type=r["alarm_type"],
                severity=r["severity"], message_tpl=r["message_tpl"],
                metric_key=r["metric_key"], operator=r["operator"],
                threshold=r["threshold"], clear_threshold=r["clear_threshold"],
                dwell_samples=r["dwell_samples"], dwell_seconds=r["dwell_seconds"],
                clear_dwell_samples=r["clear_dwell_samples"],
                device_types=tuple(r["device_types"] or ()),
                stale_after_s=r["stale_after_s"], enabled=r["enabled"],
            )
            for r in rows
        ]
        by_metric: dict[str, list[Rule]] = {}
        for rule in rules:
            if rule.metric_key:
                by_metric.setdefault(rule.metric_key, []).append(rule)
        self._rules, self._by_metric = rules, by_metric
        self._loaded_at = time.monotonic()
        log.info("alarm rules loaded", rules=len(rules), metric_rules=len(by_metric))

    def rules_for_metric(self, metric: str, device_type: str) -> list[Rule]:
        return [r for r in self._by_metric.get(metric, ())
                if r.applies_to(device_type)]

    # ------------------------------------------------------- dwell state

    async def _load_dwell(self, fields: list[str]) -> dict[str, DwellState]:
        if not fields:
            return {}
        raw = await self._redis.hmget(DWELL_HASH, fields)
        return {f: DwellState.from_wire(v) for f, v in zip(fields, raw, strict=True)}

    async def _save_dwell(self, updates: dict[str, DwellState],
                          drop: list[str]) -> None:
        pipe = self._redis.pipeline()
        if updates:
            pipe.hset(DWELL_HASH, mapping={k: v.to_wire()
                                           for k, v in updates.items()})
        if drop:
            pipe.hdel(DWELL_HASH, *drop)
        pipe.expire(DWELL_HASH, DWELL_TTL_S)
        await pipe.execute()

    # -------------------------------------------------------- thresholds

    async def evaluate_samples(self, session: AsyncSession,
                               samples: list[dict[str, Any]]) -> list[AlarmAction]:
        """Run every threshold rule over one batch of enriched samples.

        `samples` are dicts with device_id, device_type, metric, instance,
        value, observed_at, endpoint_id.
        """
        await self.ensure_rules(session)
        if not self._by_metric:
            return []

        work: list[tuple[Rule, AlarmKey, dict]] = []
        for s in samples:
            for rule in self.rules_for_metric(s["metric"], s.get("device_type", "")):
                key = AlarmKey(s["device_id"], rule.alarm_type, s.get("instance", ""))
                work.append((rule, key, s))
        if not work:
            return []

        fields = [f"{r.id}|{k.redis_field()}" for r, k, _ in work]
        states = await self._load_dwell(fields)

        actions: list[AlarmAction] = []
        updates: dict[str, DwellState] = {}
        drop: list[str] = []
        touched_devices: set[str] = set()

        for (rule, key, s), field in zip(work, fields, strict=True):
            outcome, new_state = evaluate(
                rule, key, float(s["value"]), s["observed_at"],
                states.get(field, DwellState()), s.get("endpoint_id"))

            if new_state == DwellState():
                drop.append(field)
            else:
                updates[field] = new_state

            if outcome is None:
                continue
            if isinstance(outcome, Candidate):
                action = await self._apply_candidate(session, outcome)
            else:
                action = await self._apply_clear(session, outcome, rule.alarm_type)
            if action:
                actions.append(action)
                touched_devices.add(key.device_id)

        await self._save_dwell(updates, drop)
        await repo.refresh_device_alarm_state(session, list(touched_devices))
        return actions

    # ------------------------------------------------------------ events

    async def handle_event(self, session: AsyncSession,
                           ev: dict[str, Any]) -> AlarmAction | None:
        """Turn a trap or push notification into an alarm transition.

        The backend never sees an OID: the collector already resolved the wire
        OID to a canonical event type, a severity, and - for a clear - the list
        of event types it resolves.
        """
        device_id = ev.get("device_id")
        if not device_id:
            # An unattributable trap is still recorded as an event; it just has
            # no device to hang an alarm on.
            return None

        instance = ev.get("instance") or ""
        observed = ev["observed_at"]

        if ev.get("is_clear"):
            targets = ev.get("clears") or [ev["event_type"]]
            cleared = await repo.clear_alarms(
                session, device_id=device_id, alarm_types=list(targets),
                instance=instance, at=observed, by="device")
            if not cleared:
                return None
            for row in cleared:
                await repo.record_history(session, alarm_id=row["id"],
                                          device_id=device_id, action="cleared",
                                          severity=row["severity"], actor="device")
            await repo.refresh_device_alarm_state(session, [device_id])
            return AlarmAction("alarm_cleared", cleared[0])

        severity = ev.get("severity") or "MINOR"
        if severity == "CLEAR":
            # A CLEAR severity with no clears list would otherwise raise an
            # alarm that can never be resolved.
            return None

        alarm = await repo.raise_alarm(
            session, device_id=device_id, alarm_type=ev["event_type"],
            instance=instance, severity=severity,
            message=ev.get("message") or ev["event_type"],
            source=ev.get("source") or "snmp_trap", observed_at=observed,
            endpoint_id=ev.get("endpoint_id"))
        if alarm is None or alarm["change"] == "touched":
            return None
        await repo.record_history(session, alarm_id=alarm["id"], device_id=device_id,
                                  action="raised" if alarm["change"] == "created"
                                  else alarm["change"],
                                  severity=alarm["severity"], actor="device")
        await repo.refresh_device_alarm_state(session, [device_id])
        return AlarmAction(
            "alarm_created" if alarm["change"] == "created" else "alarm_updated",
            alarm)

    # ---------------------------------------------------- communication

    async def handle_endpoint_state(self, session: AsyncSession, *, device_id: str,
                                    endpoint_id: str, status: str, protocol: str,
                                    device_name: str,
                                    last_error: str | None) -> AlarmAction | None:
        """Communication alarms come from the collector's verdict, not a rule.

        The collector already applied the debounce - it is the only thing that
        knows a poll timed out - so this does not re-debounce. Re-running dwell
        here would delay the alarm twice over.
        """
        await self.ensure_rules(session)
        rule = next((r for r in self._rules
                     if r.alarm_type == "endpoint_unreachable"), None)
        severity = rule.severity if rule else "MAJOR"
        now = datetime.now(UTC)

        if status == "ONLINE":
            cleared = await repo.clear_alarms(
                session, device_id=device_id, alarm_types=["endpoint_unreachable"],
                instance=endpoint_id, at=now, by="system")
            if not cleared:
                return None
            released_devices: list[str] = []
            for row in cleared:
                await repo.record_history(session, alarm_id=row["id"],
                                          device_id=device_id, action="cleared",
                                          severity=row["severity"], actor="system")
                # This alarm may have been explaining others. They stay broken
                # after it clears, so they have to become visible again.
                for sym in await correlation.release_symptoms(session, row["id"]):
                    released_devices.append(sym["device_id"])
                    await repo.record_history(
                        session, alarm_id=sym["id"], device_id=sym["device_id"],
                        action="released", severity=sym["severity"],
                        actor="system", detail={"root": row["id"]})
            await repo.refresh_device_alarm_state(
                session, [device_id, *released_devices])
            return AlarmAction("alarm_cleared", cleared[0])

        if status != "OFFLINE":
            # DEGRADED is one failed poll. Alarming on it produces a storm every
            # night; OFFLINE is the collector's considered verdict.
            return None

        message = (rule.message_tpl.format(device=device_name, protocol=protocol)
                   if rule else f"No response from {device_name} over {protocol}")
        if last_error:
            message = f"{message} ({last_error})"

        alarm = await repo.raise_alarm(
            session, device_id=device_id, alarm_type="endpoint_unreachable",
            instance=endpoint_id, severity=severity, message=message,
            source="comm", observed_at=now, endpoint_id=endpoint_id,
            rule_id=rule.id if rule else None)
        if alarm is None or alarm["change"] == "touched":
            return None
        await repo.record_history(session, alarm_id=alarm["id"], device_id=device_id,
                                  action="raised", severity=alarm["severity"],
                                  actor="system")

        # An unreachable device is the commonest symptom there is: the thing
        # carrying the poll may be what actually failed.
        root = await correlation.correlate(
            session, alarm_id=alarm["id"], device_id=device_id,
            alarm_type="endpoint_unreachable")
        if root:
            alarm["is_symptom"] = True
            alarm["root_cause_alarm_id"] = root["id"]
            await repo.record_history(
                session, alarm_id=alarm["id"], device_id=device_id,
                action="suppressed", severity=alarm["severity"], actor="system",
                detail={"root": root["id"], "layer": root["layer"],
                        "root_device": root["device_name"]})

        await repo.refresh_device_alarm_state(session, [device_id])
        return AlarmAction("alarm_created", alarm)

    # ----------------------------------------------------------- helpers

    async def _apply_candidate(self, session: AsyncSession,
                               c: Candidate) -> AlarmAction | None:
        alarm = await repo.raise_alarm(
            session, device_id=c.key.device_id, alarm_type=c.key.alarm_type,
            instance=c.key.instance, severity=c.severity, message=c.message,
            source=c.source, observed_at=c.observed_at, rule_id=c.rule_id,
            endpoint_id=c.endpoint_id, metric_key=c.metric_key,
            value=c.value, threshold=c.threshold)
        if alarm is None or alarm["change"] == "touched":
            return None
        await repo.record_history(
            session, alarm_id=alarm["id"], device_id=c.key.device_id,
            action="raised" if alarm["change"] == "created" else alarm["change"],
            severity=alarm["severity"], actor="system",
            detail={"value": c.value, "threshold": c.threshold})

        # Cheap for the common case: correlate() returns immediately for any
        # type that is not a "cannot see it" alarm, before touching the
        # database, so a temperature threshold pays nothing for this.
        root = await correlation.correlate(
            session, alarm_id=alarm["id"], device_id=c.key.device_id,
            alarm_type=c.key.alarm_type)
        if root:
            alarm["is_symptom"] = True
            alarm["root_cause_alarm_id"] = root["id"]
            await repo.record_history(
                session, alarm_id=alarm["id"], device_id=c.key.device_id,
                action="suppressed", severity=alarm["severity"], actor="system",
                detail={"root": root["id"], "layer": root["layer"],
                        "root_device": root["device_name"]})

        return AlarmAction(
            "alarm_created" if alarm["change"] == "created" else "alarm_updated",
            alarm)

    async def _apply_clear(self, session: AsyncSession, c: ClearSignal,
                           alarm_type: str) -> AlarmAction | None:
        cleared = await repo.clear_alarms(
            session, device_id=c.key.device_id, alarm_types=[alarm_type],
            instance=c.key.instance, at=c.observed_at, by=c.by)
        if not cleared:
            return None
        for row in cleared:
            await repo.record_history(session, alarm_id=row["id"],
                                      device_id=c.key.device_id, action="cleared",
                                      severity=row["severity"], actor=c.by)
            for sym in await correlation.release_symptoms(session, row["id"]):
                await repo.record_history(
                    session, alarm_id=sym["id"], device_id=sym["device_id"],
                    action="released", severity=sym["severity"], actor=c.by,
                    detail={"root": row["id"]})
        return AlarmAction("alarm_cleared", cleared[0])



    async def sweep_staleness(self, session: AsyncSession) -> list[AlarmAction]:
        """Raise or clear telemetry_stale across the fleet.

        A sweep rather than an event handler, because the condition is the
        ABSENCE of data. Nothing arrives to trigger it - that is the whole
        point - so something has to go and look.
        """
        await self.ensure_rules(session)
        rule = next((r for r in self._rules
                     if r.alarm_type == staleness.ALARM_TYPE), None)
        severity = rule.severity if rule else "WARNING"
        now = datetime.now(UTC)

        silent = await staleness.find_silent(session)
        silent_ids = {r["endpoint_id"] for r in silent}
        actions: list[AlarmAction] = []
        touched: set[str] = set()

        for row in silent:
            alarm = await repo.raise_alarm(
                session, device_id=row["device_id"],
                alarm_type=staleness.ALARM_TYPE, instance=row["endpoint_id"],
                severity=severity, message=staleness.message(row),
                source="staleness", observed_at=now,
                endpoint_id=row["endpoint_id"],
                rule_id=rule.id if rule else None)
            if alarm is None or alarm["change"] == "touched":
                continue
            touched.add(row["device_id"])
            await repo.record_history(
                session, alarm_id=alarm["id"], device_id=row["device_id"],
                action="raised", severity=alarm["severity"], actor="system",
                detail={"silent_s": row["silent_s"], "grace_s": row["grace_s"]})

            # Not correlated, on purpose. Every endpoint in this list is
            # polling successfully, so no upstream visibility failure explains
            # its silence - and folding it under one would hide the fault.
            actions.append(AlarmAction("alarm_created", alarm))

        # Clear the ones that started talking again. Scoped to alarms this
        # sweep raised, so it cannot clear anything else.
        open_stale = await repo.open_alarms_of_type(session, staleness.ALARM_TYPE)
        for row in open_stale:
            if row["instance"] in silent_ids:
                continue
            cleared = await repo.clear_alarms(
                session, device_id=row["device_id"],
                alarm_types=[staleness.ALARM_TYPE], instance=row["instance"],
                at=now, by="system")
            for c in cleared:
                touched.add(row["device_id"])
                await repo.record_history(
                    session, alarm_id=c["id"], device_id=row["device_id"],
                    action="cleared", severity=c["severity"], actor="system")
                for sym in await correlation.release_symptoms(session, c["id"]):
                    touched.add(sym["device_id"])
                actions.append(AlarmAction("alarm_cleared", c))

        if touched:
            await repo.refresh_device_alarm_state(session, sorted(touched))
        if actions:
            log.info("staleness sweep", silent=len(silent), changes=len(actions))
        return actions



def event_row(ev: dict[str, Any]) -> dict[str, Any]:
    """Shape a canonical event for the `event` table."""
    return {
        "ts": ev["observed_at"],
        "device_id": ev.get("device_id") or None,
        "endpoint_id": ev.get("endpoint_id") or None,
        "source_ip": ev.get("source_ip") or None,
        "event_type": ev["event_type"],
        "source": ev.get("source") or "snmp_trap",
        "severity": ev.get("severity") or "INFO",
        "message": ev.get("message") or ev["event_type"],
        "raw": json.dumps(ev.get("varbinds") or {}),
        "dedup_key": ev.get("dedup_key") or None,
    }
