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

from app.alarms import correlation, link_correlation, reconcile, staleness
from app.alarms.engine import (
    AlarmKey,
    Candidate,
    ClearSignal,
    DwellState,
    Rule,
    band_dwell_violations,
    evaluate,
)
from app.core import alert_taxonomy
from app.core.logging import get_logger
from app.repositories import alarms as repo

log = get_logger("alarms")

#: Conditions named for a percentage of nameplate that the DEVICE reports as
#: amps. Only the two PDU load bands: outlet_current_high and breaker_tripped
#: are genuinely about current and keep it, and everything else reports the
#: quantity it is named for already.
_LOAD_CONDITIONS = frozenset({"pdu_load_high", "pdu_load_critical"})

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
                device_total_only=bool(r.get("device_total_only")),
                category=r.get("category"), detection=r.get("detection"),
                response_class=r.get("response_class"),
                metric_kind=r.get("metric_kind") or "numeric",
                raise_on=bool(r.get("raise_on", True)),
                instances=tuple(r.get("instances") or ()),
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

        # Loud, and only when it is wrong. A band pair whose dwells contradict
        # each other still WORKS - the collapse hides the mess - so nothing
        # else would ever say so, and the next person to edit a dwell would
        # reintroduce it in silence.
        for low, high in band_dwell_violations(rules):
            log.warning("band dwells contradict each other",
                        lower=low, higher=high,
                        detail="the lower band waits longer than the higher, so "
                               "the more severe alarm is raised first")

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
            instance = s.get("instance", "") or ""
            # What the SOURCE called this reading, which is not always a part
            # of the device: "ALL" is a Cisco CPU table index, "CPU Temp" is a
            # BMC's label for the only CPU sensor it has. A device-scoped
            # metric files its alarm at the device so that a trap and a rule
            # watching the same thing land on the same alarm.
            instance = alert_taxonomy.alarm_instance(
                s.get("metric") or s.get("metric_key"), instance)
            for rule in self.rules_for_metric(s["metric"], s.get("device_type", "")):
                # A branch circuit is part of the feed above it, not a separate
                # thing that can fail on its own, so a rule that says so is
                # evaluated on the device total alone.
                if not rule.applies_to_instance(instance):
                    continue
                key = AlarmKey(s["device_id"], rule.alarm_type, instance)
                work.append((rule, key, s))
        if not work:
            return []

        # In sample order, not arrival order. Dwell counts CONSECUTIVE samples,
        # so evaluating them out of order would count a recovery before the
        # breach it followed.
        work.sort(key=lambda item: item[2]["observed_at"])

        fields = [f"{r.id}|{k.redis_field()}" for r, k, _ in work]
        states = await self._load_dwell(fields)

        actions: list[AlarmAction] = []
        # Progress WITHIN this batch, threaded from one sample to the next.
        #
        # Reading `states` for every item instead - the batch-start snapshot -
        # meant two samples of the same key in one batch both started from the
        # same dwell count and the last write won, so dwell counted BATCHES
        # rather than samples. Numeric rules hid it, because a 120 s poll rarely
        # puts two samples of one key in a 60 s batch. A BACnet point polled
        # every 10 s puts five or six in, so it could never reach dwell 2 and a
        # fault asserted for 20 seconds raised nothing at all.
        progress: dict[str, DwellState] = {}
        touched_devices: set[str] = set()

        for (rule, key, s), field in zip(work, fields, strict=True):
            outcome, new_state = evaluate(
                rule, key, float(s["value"]), s["observed_at"],
                progress.get(field, states.get(field, DwellState())),
                s.get("endpoint_id"))

            progress[field] = new_state

            if outcome is None:
                continue
            if isinstance(outcome, Candidate):
                action = await self._apply_candidate(session, outcome)
            else:
                action = await self._apply_clear(session, outcome, rule.alarm_type)
            if action:
                actions.append(action)
                touched_devices.add(key.device_id)

        # A key whose progress came back to zero is dropped rather than stored:
        # the hash would otherwise grow a row per key per rule for ever.
        updates = {f: st for f, st in progress.items() if st != DwellState()}
        drop = [f for f, st in progress.items() if st == DwellState()]
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

        instance = alert_taxonomy.alarm_instance(
            ev.get("metric"), ev.get("instance") or "")
        observed = ev["observed_at"]

        if ev.get("is_clear"):
            # Canonical on BOTH sides. A recovery trap that names the trap's own
            # vocabulary must clear the row the raise actually created, or the
            # alarm stays open with the device reporting itself healthy.
            targets = [alert_taxonomy.canonical_alarm_type(t)
                       for t in (ev.get("clears") or [ev["event_type"]])]
            cleared = await repo.clear_alarms(
                session, device_id=device_id, alarm_types=list(targets),
                instance=instance, at=observed, by="device")
            if not cleared:
                return None
            for row in cleared:
                await repo.record_history(session, alarm_id=row["id"],
                                          device_id=device_id, action="cleared",
                                          severity=row["severity"], actor="device")
                # A root cleared by its own recovery trap has to let go of what
                # it was explaining, exactly as a rule-cleared root does. Only
                # the rule path did this, so anything folded under a
                # trap-raised root - the far end of a link, the lower band of a
                # measurement - stayed suppressed after the root recovered,
                # invisible and with nothing left to clear it.
                for sym in await correlation.release_symptoms(session, row["id"]):
                    await repo.record_history(
                        session, alarm_id=sym["id"], device_id=sym["device_id"],
                        action="released", severity=sym["severity"],
                        actor="device", detail={"root": row["id"]})
            await link_correlation.refresh_link_state(
                session, device_id=device_id, instance=instance)
            await repo.refresh_device_alarm_state(session, [device_id])
            return AlarmAction("alarm_cleared", cleared[0])

        if ev["event_type"] == "unknown_trap":
            # An OID this platform does not map is a gap in the MAPPING, not a
            # condition on the equipment. The collector is right to record it -
            # it lands as an INFO event carrying the raw OID, which is how the
            # gap becomes visible - but raising an alarm for it manufactures a
            # row that nothing can ever clear: there is no rule behind it and no
            # recovery trap names it.
            #
            # Measured: a CPU recovery arrived as 1.3.6.1.4.1.99999.1.37, was
            # unmapped, and became a permanent INFO alarm on a device that had
            # just recovered. Every unmapped vendor trap did the same, so the
            # console filled with immortal rows in proportion to how much of the
            # estate this platform did not yet understand.
            #
            # The event is already stored by the caller. Nothing is lost here
            # except an alarm that should never have existed.
            log.info("unmapped trap recorded as an event only",
                     device_id=device_id, message=ev.get("message"))
            return None

        severity = ev.get("severity") or "MINOR"
        if severity == "CLEAR":
            # A CLEAR severity with no clears list would otherwise raise an
            # alarm that can never be resolved.
            return None

        # A condition named for a quantity the device cannot report gets the
        # platform's own measurement instead.
        #
        # A rack PDU load notification carries AMPS - rPDULoadStatusLoad is
        # tenths of an amp, Raritan sends its inlet sensor value - because that
        # is what the MIBs define. Neither vendor has a percent object. But the
        # condition is "this strip is heavily loaded", and 32.4 A does not answer
        # that; it answers "am I near the breaker", which is the OTHER alarm on
        # the same device. So the console showed Load High beside a figure that
        # looked comfortable, and the number that actually tripped it - percent
        # of nameplate - never reached an operator.
        #
        # load_pct is derived here from polled power and the nameplate, so it is
        # ours to attach. Taken at RAISE time and stored, not joined at render:
        # the alarm records a moment, and a cleared row must not drift to today's
        # reading. The amps stay on the event and in the varbinds, which is the
        # faithful record of what the device said.
        alarm_type = alert_taxonomy.canonical_alarm_type(ev["event_type"])
        metric_key, value = ev.get("metric"), ev.get("value")
        if alarm_type in _LOAD_CONDITIONS:
            derived = await repo.latest_reading(session, device_id, "load_pct")
            if derived is not None:
                metric_key, value = "load_pct", derived

        alarm = await repo.raise_alarm(
            session, device_id=device_id,
            alarm_type=alarm_type,
            instance=instance, severity=severity,
            message=ev.get("message") or ev["event_type"],
            source=ev.get("source") or "snmp_trap", observed_at=observed,
            endpoint_id=ev.get("endpoint_id"),
            # Carried onto the alarm so the condition can be verified later -
            # and so the row reaches an operator with a number on it rather
            # than a bare condition name.
            metric_key=metric_key, value=value,
            threshold=ev.get("threshold"))
        if alarm is None or alarm["change"] == "touched":
            return None
        await repo.record_history(session, alarm_id=alarm["id"], device_id=device_id,
                                  action="raised" if alarm["change"] == "created"
                                  else alarm["change"],
                                  severity=alarm["severity"], actor="device")

        # A trap and a poll rule can raise different bands of one measurement -
        # the trap fires at the vendor's threshold, the rule at ours - so the
        # collapse has to happen on both paths or the console shows one row
        # from each.
        await self._collapse_bands(
            session, alarm, device_id=device_id,
            alarm_type=alert_taxonomy.canonical_alarm_type(ev["event_type"]),
            instance=instance, actor="device")
        await self._pair_link_ends(
            session, alarm, device_id=device_id,
            alarm_type=alert_taxonomy.canonical_alarm_type(ev["event_type"]),
            instance=instance, actor="device")
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
            rule_id=rule.id if rule else None,
            response_class=rule.response_class if rule else None)
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

    async def _collapse_bands(self, session, alarm: dict, *, device_id: str,
                              alarm_type: str, instance: str,
                              actor: str) -> None:
        """Fold this alarm and its band siblings into one visible row.

        A warning rule and a critical rule on one measurement are two views of
        one condition. Both stay in the record - they carry different
        thresholds, dwells and response classes, and both are true - but only
        the higher one is shown, the same way a dependency symptom is folded
        under its root.
        """
        root = await correlation.collapse_bands(
            session, alarm_id=alarm["id"], device_id=device_id,
            alarm_type=alarm_type, instance=instance)
        reason = f"lower band of {root['alarm_type']}" if root else ""

        if not root:
            # Same condition, one detector naming the part and the other not.
            # A trap says "this device is hot" with no instance; the rule
            # watching the same reading says "CPU Temp is hot". On a metric
            # whose instances are real they cannot share an alarm key - and
            # must not, since two CPU sensors are two faults - so the part-less
            # one is folded under the one that names the part.
            root = await correlation.collapse_unqualified(
                session, alarm_id=alarm["id"], device_id=device_id,
                alarm_type=alarm_type, instance=instance)
            if root:
                reason = (f"{root['alarm_type']} on {root['instance']}, "
                          f"reported without naming the part")
        if not root:
            return
        alarm["is_symptom"] = True
        alarm["root_cause_alarm_id"] = root["id"]
        await repo.record_history(
            session, alarm_id=alarm["id"], device_id=device_id,
            action="suppressed", severity=alarm["severity"], actor=actor,
            detail={"root": root["id"], "reason": reason})

    async def _pair_link_ends(self, session, alarm: dict, *, device_id: str,
                              alarm_type: str, instance: str,
                              actor: str) -> None:
        """Fold the far end of a failed link under the near one.

        Both ends of a cable report it, and both reports are real. The later
        one becomes a symptom of the earlier so the console shows one row for
        one cable, and that row names both ends. Nothing is discarded: a link
        where only ONE end ever reports stays exactly as it is, because that
        asymmetry is a diagnosis - a dead far end, or a unidirectional fibre.
        """
        if alarm_type not in link_correlation.LINK_TYPES:
            return
        state = await link_correlation.refresh_link_state(
            session, device_id=device_id, instance=instance)
        if alarm.get("is_symptom"):
            # Already explained by something upstream - a dead peer, a failed
            # feed. That root outranks the far end of the cable.
            return
        root = await link_correlation.pair_ends(
            session, alarm_id=alarm["id"], device_id=device_id,
            instance=instance)
        if not root:
            return
        alarm["is_symptom"] = True
        alarm["root_cause_alarm_id"] = root["id"]
        await repo.record_history(
            session, alarm_id=alarm["id"], device_id=device_id,
            action="suppressed", severity=alarm["severity"], actor=actor,
            detail={"root": root["id"], "link": root["connection_id"],
                    "reason": f"far end of the same link, reported first by "
                              f"{root['device_name']} {root['port']}",
                    "oper_state": state})

    # ----------------------------------------------------------- helpers

    async def _apply_candidate(self, session: AsyncSession,
                               c: Candidate) -> AlarmAction | None:
        alarm = await repo.raise_alarm(
            session, device_id=c.key.device_id, alarm_type=c.key.alarm_type,
            instance=c.key.instance, severity=c.severity, message=c.message,
            source=c.source, observed_at=c.observed_at, rule_id=c.rule_id,
            endpoint_id=c.endpoint_id, metric_key=c.metric_key,
            value=c.value, threshold=c.threshold,
            category=c.category, response_class=c.response_class)
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

        # Bands, once the dependency question is settled. A warning and a
        # critical on ONE measurement are two views of one condition, and an
        # alarm already folded under an upstream root is not folded again.
        if not alarm.get("is_symptom"):
            await self._collapse_bands(
                session, alarm, device_id=c.key.device_id,
                alarm_type=c.key.alarm_type, instance=c.key.instance,
                actor="system")
        await self._pair_link_ends(
            session, alarm, device_id=c.key.device_id,
            alarm_type=c.key.alarm_type, instance=c.key.instance,
            actor="system")

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
        await link_correlation.refresh_link_state(
            session, device_id=c.key.device_id, instance=c.key.instance)
        return AlarmAction("alarm_cleared", cleared[0])



    async def sweep_dead_endpoints(self, session: AsyncSession) -> list[AlarmAction]:
        """Clear alarms whose endpoint has been retired or removed.

        An endpoint alarm clears when the endpoint polls successfully again. A
        disabled endpoint never polls, so without this its alarms are permanent
        - 52 of them survived an import that correctly decided those device
        types do not speak gNMI, and no operator action could have cleared them.

        The alarm is cleared rather than deleted: it happened, and the history
        should say so, including that it ended because the endpoint went away
        rather than because anything recovered.
        """
        now = datetime.now(UTC)
        orphans = await repo.open_alarms_on_dead_endpoints(session)
        actions: list[AlarmAction] = []
        touched: set[str] = set()

        for row in orphans:
            reason = ("endpoint removed" if row["endpoint_missing"]
                      else "endpoint retired")
            cleared = await repo.clear_alarms(
                session, device_id=row["device_id"],
                alarm_types=[row["alarm_type"]], instance=row["instance"],
                at=now, by=f"system:{reason}")
            for c in cleared:
                touched.add(row["device_id"])
                await repo.record_history(
                    session, alarm_id=c["id"], device_id=row["device_id"],
                    action="cleared", severity=c["severity"], actor="system",
                    detail={"reason": reason})
                for sym in await correlation.release_symptoms(session, c["id"]):
                    touched.add(sym["device_id"])
                actions.append(AlarmAction("alarm_cleared", c))

        if touched:
            await repo.refresh_device_alarm_state(session, sorted(touched))
        if actions:
            log.info("cleared alarms on retired endpoints", alarms=len(actions))
        return actions

    async def sweep_trap_reconciliation(self, session: AsyncSession
                                        ) -> list[AlarmAction]:
        """Close trap alarms the measurement, or the silence, has outlived.

        A trap is one UDP datagram: the clear that ends a condition is as
        losable as the raise that started it, and losing the clear leaves an
        alarm nothing can resolve. Measured on this fleet - a CPUNormal fired
        into a 33-second window while the collector restarted, and three alarms
        stood open on a machine the platform could see was at 39.9%.

        So the poll gets the last word, in the two ways it can have one.
        """
        now = datetime.now(UTC)
        actions: list[AlarmAction] = []
        touched: set[str] = set()

        async def close(row: dict, reason: str, note: str) -> None:
            cleared = await repo.clear_alarms(
                session, device_id=row["device_id"],
                alarm_types=[row["alarm_type"]], instance="",
                at=now, by=f"reconciliation:{reason}")
            for c in cleared:
                touched.add(row["device_id"])
                await repo.record_history(
                    session, alarm_id=c["id"], device_id=row["device_id"],
                    action="cleared", severity=c["severity"],
                    actor="reconciliation", detail={"reason": note})
                # A folded band or a dependency symptom comes back into view
                # when its root goes, exactly as it would on any other clear.
                for sym in await correlation.release_symptoms(session, c["id"]):
                    touched.add(sym["device_id"])
                actions.append(AlarmAction("alarm_cleared", c))
            if cleared:
                log.info("trap alarm reconciled", device=row["device_name"],
                         alarm_type=row["alarm_type"], reason=reason,
                         detail=note)

        for row in await reconcile.measured_clear(session):
            await close(row, "measured", reconcile.measured_reason(row))

        for row in await reconcile.aged_out(session):
            await close(row, "aged", reconcile.aged_reason(row))

        if touched:
            await repo.refresh_device_alarm_state(session, sorted(touched))
        return actions

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
                rule_id=rule.id if rule else None,
                response_class=rule.response_class if rule else None)
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
