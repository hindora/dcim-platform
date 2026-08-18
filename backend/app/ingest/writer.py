"""The only code in the system that writes telemetry rows.

Numeric samples go in via COPY into a per-transaction TEMP table followed by
INSERT ... SELECT ... ON CONFLICT DO NOTHING. COPY alone cannot express a
conflict policy, and at-least-once delivery guarantees we will eventually see a
redelivered batch. The extra pass costs roughly 15% and removes a whole class of
incident.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class SampleRow:
    ts: datetime
    device_id: str
    metric_id: int
    instance: str
    value: float
    quality: str


@dataclass(slots=True)
class BoolRow:
    ts: datetime
    device_id: str
    metric_id: int
    instance: str
    value: bool
    quality: str


@dataclass(slots=True)
class HotUpdate:
    device_id: str
    last_seen: datetime
    metrics: dict            # {"metric_key": {"v":..., "t": iso, "q": "good"}}
    power_w: float | None = None
    inlet_temp_c: float | None = None
    cpu_util_pct: float | None = None
    humidity_pct: float | None = None


async def _raw_asyncpg(session: AsyncSession):
    """The underlying asyncpg connection, for COPY."""
    conn = await session.connection()
    raw = await conn.get_raw_connection()
    return raw.driver_connection


async def copy_samples(session: AsyncSession, rows: list[SampleRow]) -> int:
    if not rows:
        return 0
    pg = await _raw_asyncpg(session)
    await pg.execute("""
        CREATE TEMP TABLE IF NOT EXISTS _incoming_sample (
            ts timestamptz, device_id uuid, metric_id smallint,
            instance text, value double precision, quality text
        ) ON COMMIT DROP
    """)
    await pg.execute("TRUNCATE _incoming_sample")
    await pg.copy_records_to_table(
        "_incoming_sample",
        records=[(r.ts, r.device_id, r.metric_id, r.instance, r.value, r.quality)
                 for r in rows],
        columns=["ts", "device_id", "metric_id", "instance", "value", "quality"],
    )
    await pg.execute("""
        INSERT INTO telemetry_sample (ts, device_id, metric_id, instance, value, quality)
        SELECT ts, device_id, metric_id, instance, value, quality FROM _incoming_sample
        ON CONFLICT DO NOTHING
    """)
    return len(rows)


async def insert_bools(session: AsyncSession, rows: list[BoolRow]) -> int:
    """Binary points are sparse - written on change plus a heartbeat - so a
    plain executemany is cheaper than setting up a COPY."""
    if not rows:
        return 0
    await session.execute(
        text("""
            INSERT INTO telemetry_bool (ts, device_id, metric_id, instance, value, quality)
            VALUES (:ts, CAST(:device_id AS uuid), :metric_id, :instance, :value, :quality)
            ON CONFLICT DO NOTHING
        """),
        [{"ts": r.ts, "device_id": r.device_id, "metric_id": r.metric_id,
          "instance": r.instance, "value": r.value, "quality": r.quality} for r in rows],
    )
    return len(rows)


async def upsert_device_state(session: AsyncSession, updates: list[HotUpdate]) -> int:
    """Merge hot metrics into device_state.

    The ``WHERE ... <= EXCLUDED.updated_at`` guard is what stops a late
    redelivery from overwriting newer state with older values.
    """
    if not updates:
        return 0
    now = datetime.now(UTC)
    await session.execute(
        text("""
            INSERT INTO device_state (device_id, status, last_seen, metrics,
                                      power_w, inlet_temp_c, cpu_util_pct, humidity_pct,
                                      updated_at)
            VALUES (CAST(:device_id AS uuid), 'ONLINE', :last_seen,
                    CAST(:metrics AS jsonb),
                    :power_w, :inlet_temp_c, :cpu_util_pct, :humidity_pct, :now)
            ON CONFLICT (device_id) DO UPDATE SET
                last_seen    = GREATEST(device_state.last_seen, EXCLUDED.last_seen),
                metrics      = device_state.metrics || EXCLUDED.metrics,
                power_w      = COALESCE(EXCLUDED.power_w,      device_state.power_w),
                inlet_temp_c = COALESCE(EXCLUDED.inlet_temp_c, device_state.inlet_temp_c),
                cpu_util_pct = COALESCE(EXCLUDED.cpu_util_pct, device_state.cpu_util_pct),
                humidity_pct = COALESCE(EXCLUDED.humidity_pct, device_state.humidity_pct),
                updated_at   = EXCLUDED.updated_at
            WHERE device_state.updated_at IS NULL
               OR device_state.updated_at <= EXCLUDED.updated_at
        """),
        [{"device_id": u.device_id, "last_seen": u.last_seen,
          "metrics": json.dumps(u.metrics),
          "power_w": u.power_w, "inlet_temp_c": u.inlet_temp_c,
          "cpu_util_pct": u.cpu_util_pct, "humidity_pct": u.humidity_pct,
          "now": now} for u in updates],
    )
    return len(updates)


async def record_poll_results(session: AsyncSession, rows: list[dict]) -> int:
    if not rows:
        return 0
    await session.execute(
        text("""
            INSERT INTO poll_result (ts, endpoint_id, collector_id, success,
                                     latency_ms, error_class, metrics_returned)
            VALUES (:ts, CAST(:endpoint_id AS uuid), :collector_id, :success,
                    :latency_ms, :error_class, :metrics_returned)
        """),
        rows,
    )
    return len(rows)


async def apply_endpoint_state(session: AsyncSession, s: dict) -> None:
    """Persist a communication-state transition and re-derive device status.

    Device status is the best of its endpoints: a server whose BMC is
    unreachable but whose OS agent answers is DEGRADED, not OFFLINE. Reporting
    it as OFFLINE would send an operator to a machine that is running fine.
    """
    await session.execute(
        text("""
            INSERT INTO endpoint_state (endpoint_id, status, last_success, last_failure,
                                        consecutive_failures, last_error, last_error_class,
                                        last_latency_ms, collector_id, last_seen, updated_at)
            VALUES (CAST(:endpoint_id AS uuid), CAST(:status AS comm_status_t),
                    :last_success, :last_failure, :consecutive_failures,
                    :last_error, :last_error_class, :latency_ms, :collector_id,
                    :last_seen, now())
            ON CONFLICT (endpoint_id) DO UPDATE SET
                status               = EXCLUDED.status,
                last_success         = COALESCE(EXCLUDED.last_success, endpoint_state.last_success),
                last_failure         = COALESCE(EXCLUDED.last_failure, endpoint_state.last_failure),
                consecutive_failures = EXCLUDED.consecutive_failures,
                last_error           = EXCLUDED.last_error,
                last_error_class     = EXCLUDED.last_error_class,
                last_latency_ms      = EXCLUDED.last_latency_ms,
                collector_id         = EXCLUDED.collector_id,
                last_seen            = GREATEST(endpoint_state.last_seen, EXCLUDED.last_seen),
                updated_at           = now()
        """),
        s,
    )
    await session.execute(
        text("""
            WITH agg AS (
                SELECT e.device_id,
                       MIN(CASE es.status WHEN 'ONLINE' THEN 0 WHEN 'DEGRADED' THEN 1
                                          WHEN 'UNKNOWN' THEN 2 WHEN 'OFFLINE' THEN 3
                                          ELSE 4 END) AS best,
                       MAX(es.last_seen) AS last_seen
                FROM device_endpoint e
                JOIN endpoint_state es ON es.endpoint_id = e.id
                WHERE e.enabled AND e.device_id = (
                    SELECT device_id FROM device_endpoint WHERE id = CAST(:endpoint_id AS uuid))
                GROUP BY e.device_id
            )
            INSERT INTO device_state (device_id, status, last_seen, updated_at)
            SELECT device_id,
                   (ARRAY['ONLINE','DEGRADED','UNKNOWN','OFFLINE','DISABLED'])[best + 1]
                       ::comm_status_t,
                   last_seen, now()
            FROM agg
            ON CONFLICT (device_id) DO UPDATE SET
                status     = EXCLUDED.status,
                last_seen  = GREATEST(device_state.last_seen, EXCLUDED.last_seen),
                updated_at = now()
        """),
        {"endpoint_id": s["endpoint_id"]},
    )
