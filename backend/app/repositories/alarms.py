"""Alarm, event and rule SQL.

Raise, update and clear are all idempotent: they key on the unique partial index
`(device_id, alarm_type, instance) WHERE state <> 'CLEARED'`, so a redelivered
message updates the existing alarm instead of creating a second one.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ------------------------------------------------------------------- rules

async def load_rules(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id::text, name, alarm_type, metric_key, operator,
               threshold::float8 AS threshold,
               clear_threshold::float8 AS clear_threshold,
               dwell_samples, dwell_seconds, clear_dwell_samples,
               severity::text AS severity, device_types, message_tpl,
               stale_after_s, enabled
        FROM alarm_rule WHERE enabled
    """))).mappings().all()
    return [dict(r) for r in rows]


async def list_rules(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id::text, name, alarm_type, metric_key, operator,
               threshold::float8 AS threshold,
               clear_threshold::float8 AS clear_threshold,
               dwell_samples, dwell_seconds, clear_dwell_samples,
               severity::text AS severity, device_types, message_tpl,
               stale_after_s, enabled
        FROM alarm_rule ORDER BY alarm_type, name
    """))).mappings().all()
    return [dict(r) for r in rows]


async def set_rule_enabled(session: AsyncSession, rule_id: str,
                           enabled: bool) -> bool:
    res = await session.execute(text("""
        UPDATE alarm_rule SET enabled = :enabled WHERE id = CAST(:id AS uuid)
        RETURNING id
    """), {"id": rule_id, "enabled": enabled})
    return res.first() is not None


# ------------------------------------------------------------------ alarms

async def raise_alarm(session: AsyncSession, *, device_id: str, alarm_type: str,
                      instance: str, severity: str, message: str, source: str,
                      observed_at: datetime, rule_id: str | None = None,
                      endpoint_id: str | None = None,
                      metric_key: str | None = None,
                      value: float | None = None,
                      threshold: float | None = None) -> dict[str, Any] | None:
    """Insert or update the open alarm for this key.

    Returns the alarm plus a `change` describing what happened - created,
    escalated, deescalated or touched - so the caller can decide what is worth
    telling a browser about. A repeat of an unchanged alarm produces no event:
    an alarm list that reshuffles every poll is unusable.
    """
    row = (await session.execute(text("""
        INSERT INTO alarm (device_id, endpoint_id, alarm_type, instance, rule_id,
                           severity, state, message, metric_key, trigger_value,
                           threshold, source, first_seen, last_seen)
        VALUES (CAST(:device_id AS uuid), CAST(:endpoint_id AS uuid), :alarm_type,
                :instance, CAST(:rule_id AS uuid), CAST(:severity AS severity_t),
                'ACTIVE', :message, :metric_key, :value, :threshold, :source,
                :observed_at, :observed_at)
        ON CONFLICT (device_id, alarm_type, instance) WHERE state <> 'CLEARED'
        DO UPDATE SET
            prev_severity    = alarm.severity,
            severity         = EXCLUDED.severity,
            message          = EXCLUDED.message,
            trigger_value    = EXCLUDED.trigger_value,
            last_seen        = GREATEST(alarm.last_seen, EXCLUDED.last_seen),
            occurrence_count = alarm.occurrence_count + 1
        RETURNING id::text, severity::text AS severity,
                  prev_severity::text AS prev_severity,
                  state::text AS state, occurrence_count, first_seen, last_seen,
                  alarm_type, instance, message, device_id::text AS device_id
    """), {
        "device_id": device_id, "endpoint_id": endpoint_id,
        "alarm_type": alarm_type, "instance": instance, "rule_id": rule_id,
        "severity": severity, "message": message, "metric_key": metric_key,
        "value": value, "threshold": threshold, "source": source,
        "observed_at": observed_at,
    })).mappings().first()
    if row is None:
        return None
    out = dict(row)
    if out["occurrence_count"] == 1:
        out["change"] = "created"
    elif out["prev_severity"] and out["prev_severity"] != out["severity"]:
        out["change"] = "escalated"
    else:
        out["change"] = "touched"
    return out


async def clear_alarms(session: AsyncSession, *, device_id: str,
                       alarm_types: list[str], instance: str,
                       at: datetime, by: str = "system") -> list[dict[str, Any]]:
    """Clear every open alarm matching the key family.

    A clear with no matching raise is normal - after a restart, or when a device
    reports recovery for something we never saw fail - and returns nothing
    rather than erroring.
    """
    rows = (await session.execute(text("""
        UPDATE alarm SET state = 'CLEARED', cleared_at = :at, cleared_by = :by
        WHERE device_id = CAST(:device_id AS uuid)
          AND alarm_type = ANY(:alarm_types)
          AND instance = :instance
          AND state <> 'CLEARED'
        RETURNING id::text, alarm_type, instance, severity::text AS severity,
                  device_id::text AS device_id, message
    """), {"device_id": device_id, "alarm_types": alarm_types,
           "instance": instance, "at": at, "by": by})).mappings().all()
    return [dict(r) for r in rows]


async def record_history(session: AsyncSession, *, alarm_id: str, device_id: str,
                         action: str, severity: str | None = None,
                         actor: str | None = None,
                         detail: dict | None = None) -> None:
    await session.execute(text("""
        INSERT INTO alarm_history (alarm_id, device_id, action, severity, actor, detail)
        VALUES (CAST(:alarm_id AS uuid), CAST(:device_id AS uuid), :action,
                CAST(:severity AS severity_t), :actor, CAST(:detail AS jsonb))
    """), {"alarm_id": alarm_id, "device_id": device_id, "action": action,
           "severity": severity, "actor": actor,
           "detail": json.dumps(detail or {})})


_ALARM_SELECT = """
    SELECT a.id::text, a.device_id::text, d.name AS device_name, d.device_type,
           a.alarm_type, a.instance, a.severity::text AS severity,
           a.state::text AS state, a.message, a.metric_key,
           a.trigger_value::float8 AS trigger_value,
           a.threshold::float8 AS threshold, a.source,
           a.first_seen, a.last_seen, a.occurrence_count,
           a.acknowledged_at, a.acknowledged_by, a.cleared_at,
           a.is_symptom, a.root_cause_alarm_id::text,
           dc.code AS datacenter_code, rm.name AS room_name, r.name AS rack_name
    FROM alarm a
    -- LEFT, not INNER. A platform alarm has no device, and an inner join here
    -- would silently drop the alarms that say the monitoring itself is broken -
    -- the ones an operator most needs to see in this list.
    LEFT JOIN device d     ON d.id = a.device_id
    LEFT JOIN rack r       ON r.id = d.rack_id
    LEFT JOIN rack_row rr  ON rr.id = r.row_id
    LEFT JOIN room rm      ON rm.id = COALESCE(rr.room_id, d.room_id)
    LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
"""


async def list_alarms(session: AsyncSession, *, states: list[str] | None = None,
                      severities: list[str] | None = None,
                      device_id: str | None = None,
                      alarm_type: str | None = None,
                      include_symptoms: bool = False,
                      limit: int = 100) -> list[dict[str, Any]]:
    where, params = [], {"limit": limit}
    if states:
        where.append("a.state::text = ANY(:states)")
        params["states"] = states
    if severities:
        where.append("a.severity::text = ANY(:severities)")
        params["severities"] = severities
    if device_id:
        where.append("a.device_id = CAST(:device_id AS uuid)")
        params["device_id"] = device_id
    if alarm_type:
        where.append("a.alarm_type = :alarm_type")
        params["alarm_type"] = alarm_type
    if not include_symptoms:
        # Roots only by default. An alarm list showing 21 rows for one OOB
        # switch failure is the reason operators stop looking at alarm lists.
        where.append("NOT a.is_symptom")

    sql = _ALARM_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += """ ORDER BY CASE a.severity
                   WHEN 'CRITICAL' THEN 0 WHEN 'MAJOR' THEN 1 WHEN 'MINOR' THEN 2
                   WHEN 'WARNING' THEN 3 WHEN 'INFO' THEN 4 ELSE 5 END,
               a.last_seen DESC LIMIT :limit"""
    rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def get_alarm(session: AsyncSession, alarm_id: str) -> dict[str, Any] | None:
    row = (await session.execute(
        text(_ALARM_SELECT + " WHERE a.id = CAST(:id AS uuid)"),
        {"id": alarm_id})).mappings().first()
    return dict(row) if row else None


async def acknowledge(session: AsyncSession, alarm_id: str, actor: str,
                      note: str | None) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        UPDATE alarm SET state = 'ACKNOWLEDGED', acknowledged_at = now(),
                         acknowledged_by = :actor, ack_note = :note
        WHERE id = CAST(:id AS uuid) AND state = 'ACTIVE'
        RETURNING id::text, device_id::text, alarm_type, severity::text AS severity
    """), {"id": alarm_id, "actor": actor, "note": note})).mappings().first()
    return dict(row) if row else None


async def manual_clear(session: AsyncSession, alarm_id: str,
                       actor: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        UPDATE alarm SET state = 'CLEARED', cleared_at = now(), cleared_by = :actor
        WHERE id = CAST(:id AS uuid) AND state <> 'CLEARED'
        RETURNING id::text, device_id::text, alarm_type, severity::text AS severity
    """), {"id": alarm_id, "actor": actor})).mappings().first()
    return dict(row) if row else None


async def summary(session: AsyncSession) -> dict[str, Any]:
    row = (await session.execute(text("""
        SELECT count(*) FILTER (WHERE state <> 'CLEARED')                  AS active,
               count(*) FILTER (WHERE state <> 'CLEARED'
                                  AND severity = 'CRITICAL')               AS critical,
               count(*) FILTER (WHERE state <> 'CLEARED'
                                  AND severity = 'MAJOR')                  AS major,
               count(*) FILTER (WHERE state <> 'CLEARED'
                                  AND severity = 'WARNING')                AS warning,
               count(*) FILTER (WHERE state = 'ACKNOWLEDGED')              AS acknowledged,
               count(*) FILTER (WHERE state <> 'CLEARED' AND is_symptom)   AS suppressed_symptoms
        FROM alarm
    """))).mappings().first()
    return dict(row) if row else {}


async def refresh_device_alarm_state(session: AsyncSession,
                                     device_ids: list[str]) -> None:
    """Re-derive device_state.health and max_severity from open alarms."""
    if not device_ids:
        return
    await session.execute(text("""
        WITH agg AS (
            SELECT d.id AS device_id,
                   COALESCE(MAX(a.severity) FILTER (WHERE a.state <> 'CLEARED'),
                            'CLEAR')::severity_t AS max_sev,
                   count(a.id) FILTER (WHERE a.state <> 'CLEARED') AS open_count
            FROM device d
            LEFT JOIN alarm a ON a.device_id = d.id
            WHERE d.id = ANY(CAST(:ids AS uuid[]))
            GROUP BY d.id
        )
        INSERT INTO device_state (device_id, max_severity, active_alarms, health, updated_at)
        SELECT device_id, max_sev, open_count,
               CASE WHEN max_sev IN ('CRITICAL','MAJOR') THEN 'CRITICAL'
                    WHEN max_sev IN ('MINOR','WARNING')  THEN 'WARNING'
                    ELSE 'OK' END::health_t,
               now()
        FROM agg
        ON CONFLICT (device_id) DO UPDATE SET
            max_severity  = EXCLUDED.max_severity,
            active_alarms = EXCLUDED.active_alarms,
            health        = EXCLUDED.health,
            updated_at    = now()
    """), {"ids": device_ids})


# ------------------------------------------------------------------ events

async def insert_events(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    await session.execute(text("""
        INSERT INTO event (ts, device_id, endpoint_id, source_ip, event_type,
                           source, severity, message, raw, dedup_key)
        VALUES (:ts, CAST(:device_id AS uuid), CAST(:endpoint_id AS uuid),
                CAST(:source_ip AS inet), :event_type, :source,
                CAST(:severity AS severity_t), :message, CAST(:raw AS jsonb),
                :dedup_key)
    """), rows)
    return len(rows)


async def list_events(session: AsyncSession, *, device_id: str | None = None,
                      event_type: str | None = None,
                      unresolved_only: bool = False,
                      limit: int = 100) -> list[dict[str, Any]]:
    where, params = ["ts > now() - interval '7 days'"], {"limit": limit}
    if device_id:
        where.append("e.device_id = CAST(:device_id AS uuid)")
        params["device_id"] = device_id
    if event_type:
        where.append("e.event_type = :event_type")
        params["event_type"] = event_type
    if unresolved_only:
        where.append("e.device_id IS NULL")

    rows = (await session.execute(text(f"""
        SELECT e.id, e.ts, e.device_id::text, d.name AS device_name,
               host(e.source_ip) AS source_ip, e.event_type, e.source,
               e.severity::text AS severity, e.message, e.raw
        FROM event e
        LEFT JOIN device d ON d.id = e.device_id
        WHERE {' AND '.join(where)}
        ORDER BY e.ts DESC LIMIT :limit
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def open_alarms_of_type(session: AsyncSession,
                              alarm_type: str) -> list[dict[str, Any]]:
    """Open alarms of one type, for a sweep that needs to clear what recovered.

    Scoped to the type so a sweep can only ever clear alarms it is responsible
    for raising.
    """
    rows = (await session.execute(text("""
        SELECT id::text, device_id::text AS device_id, instance,
               severity::text AS severity
          FROM alarm
         WHERE alarm_type = :alarm_type AND state <> 'CLEARED'
    """), {"alarm_type": alarm_type})).mappings().all()
    return [dict(r) for r in rows]


# --- platform alarms ----------------------------------------------------------
#
# Same table, same list, same acknowledge and clear paths as a device alarm.
# They differ only in having no device, which is why they need their own
# statements: `device_id = NULL` is never true, so the device-scoped clear
# silently matches nothing.


async def raise_platform_alarm(session: AsyncSession, *, alarm_type: str,
                               instance: str, severity: str, message: str,
                               observed_at: datetime,
                               value: float | None = None,
                               threshold: float | None = None,
                               source: str = "platform") -> dict[str, Any] | None:
    """Insert or update the open platform alarm for this key.

    The ON CONFLICT target resolves to the NULLS NOT DISTINCT index added in
    0013. Without that index this would insert a new row on every evaluation
    cycle, because Postgres considers two NULL device_ids distinct by default.
    """
    row = (await session.execute(text("""
        INSERT INTO alarm (device_id, alarm_type, instance, severity, state,
                           message, trigger_value, threshold, source,
                           first_seen, last_seen)
        VALUES (NULL, :alarm_type, :instance, CAST(:severity AS severity_t),
                'ACTIVE', :message, :value, :threshold, :source,
                :observed_at, :observed_at)
        ON CONFLICT (device_id, alarm_type, instance) WHERE state <> 'CLEARED'
        DO UPDATE SET
            prev_severity    = alarm.severity,
            severity         = EXCLUDED.severity,
            message          = EXCLUDED.message,
            trigger_value    = EXCLUDED.trigger_value,
            last_seen        = GREATEST(alarm.last_seen, EXCLUDED.last_seen),
            occurrence_count = alarm.occurrence_count + 1
        RETURNING id::text, severity::text AS severity,
                  prev_severity::text AS prev_severity, occurrence_count,
                  alarm_type, instance, message
    """), {"alarm_type": alarm_type, "instance": instance, "severity": severity,
           "message": message, "value": value, "threshold": threshold,
           "source": source, "observed_at": observed_at})).mappings().first()
    if row is None:
        return None
    out = dict(row)
    # "severity_changed" rather than "escalated": ingest_lag_high going from
    # CRITICAL back down to WARNING is a de-escalation, and calling it an
    # escalation would tell an operator the opposite of what happened.
    out["change"] = ("created" if out["occurrence_count"] == 1
                     else "severity_changed"
                     if out["prev_severity"] != out["severity"]
                     else "touched")
    return out


async def open_platform_alarms(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id::text, alarm_type, instance, severity::text AS severity,
               message, first_seen, last_seen, occurrence_count,
               acknowledged_at
          FROM alarm
         WHERE device_id IS NULL AND state <> 'CLEARED'
         ORDER BY last_seen DESC
    """))).mappings().all()
    return [dict(r) for r in rows]


async def clear_platform_alarms(session: AsyncSession, *,
                                keys: list[tuple[str, str]], at: datetime,
                                by: str = "system") -> list[dict[str, Any]]:
    """Clear the named platform alarms.

    Driven by absence from the current findings, because almost none of these
    conditions produce a recovery event - a collector that starts heartbeating
    again does not announce that it had stopped.
    """
    if not keys:
        return []
    # Two parallel arrays unnested into pairs, rather than a row comparison
    # against a 2-D array: `(a, b) = ANY(CAST(:keys AS text[][]))` parses but
    # fails at execution with "operator does not exist: record = text", because
    # ANY over a multidimensional array yields its scalar elements, not rows.
    rows = (await session.execute(text("""
        UPDATE alarm SET state = 'CLEARED', cleared_at = :at, cleared_by = :by
         WHERE device_id IS NULL
           AND state <> 'CLEARED'
           AND (alarm_type, instance) IN (
                 SELECT t.a, t.b
                   FROM unnest(CAST(:types AS text[]), CAST(:instances AS text[]))
                     AS t(a, b))
        RETURNING id::text, alarm_type, instance, severity::text AS severity,
                  message
    """), {"types": [k[0] for k in keys], "instances": [k[1] for k in keys],
           "at": at, "by": by})).mappings().all()
    return [dict(r) for r in rows]


async def active_alarm_counts(session: AsyncSession) -> list[dict[str, Any]]:
    """Active alarms by severity, split by whether they are about a device.

    The split is the point: "12 active alarms" reads very differently when
    three of them are saying the collector is dead and the rest are stale
    telemetry caused by that.
    """
    rows = (await session.execute(text("""
        SELECT severity::text AS severity,
               CASE WHEN device_id IS NULL THEN 'platform' ELSE 'device' END AS origin,
               count(*) AS n
          FROM alarm
         WHERE state <> 'CLEARED'
         GROUP BY 1, 2
    """))).mappings().all()
    return [dict(r) for r in rows]
