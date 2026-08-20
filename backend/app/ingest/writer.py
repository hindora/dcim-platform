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
from uuid import UUID

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
class TextRow:
    ts: datetime
    device_id: str
    metric_id: int
    instance: str
    value: str
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
    """The underlying asyncpg connection, for driver-level batch statements."""
    conn = await session.connection()
    raw = await conn.get_raw_connection()
    return raw.driver_connection


async def copy_samples(session: AsyncSession, rows: list[SampleRow]) -> int:
    """Insert numeric samples, discarding duplicates.

    This deliberately does NOT use COPY. COPY cannot express ON CONFLICT, so it
    needs a staging table, and staging through a TEMP table under SQLAlchemy's
    asyncpg wrapper silently staged zero rows - the INSERT reported "INSERT 0 0"
    while the batch looked healthy from the outside. A wrong number is worse
    than a slower one.

    asyncpg's executemany pipelines the statements, so this is still a single
    round trip's worth of work per batch, and the primary key gives us the
    idempotency that at-least-once delivery requires.
    """
    if not rows:
        return 0
    pg = await _raw_asyncpg(session)
    await pg.executemany(
        """
        INSERT INTO telemetry_sample (ts, device_id, metric_id, instance, value, quality)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT DO NOTHING
        """,
        [(r.ts, UUID(r.device_id) if isinstance(r.device_id, str) else r.device_id,
          r.metric_id, r.instance, r.value, r.quality) for r in rows],
    )
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


async def insert_texts(session: AsyncSession, rows: list[TextRow]) -> int:
    """State words - a UPS operating mode, a transfer switch position.

    These belong in their own table rather than as a number in
    telemetry_sample: "battery" is not a measurement, and encoding it as 2
    invites someone to average it.
    """
    if not rows:
        return 0
    await session.execute(
        text("""
            INSERT INTO telemetry_text (ts, device_id, metric_id, instance, value, quality)
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


_UPSERT_ENDPOINT_STATE = text("""
    INSERT INTO endpoint_state (endpoint_id, status, last_success, last_failure,
                                consecutive_failures, last_error, last_error_class,
                                last_latency_ms, collector_id, last_seen,
                                poll_count, fail_count, timeout_count,
                                auth_fail_count, updated_at)
    VALUES (CAST(:endpoint_id AS uuid), CAST(:status AS comm_status_t),
            :last_success, :last_failure, :consecutive_failures,
            :last_error, :last_error_class, :latency_ms, :collector_id,
            :last_seen, :poll_count, :fail_count, :timeout_count,
            :auth_fail_count, now())
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
        -- The collector's counters are cumulative per process, so they reset
        -- to zero when it restarts. GREATEST keeps the stored total from
        -- walking backwards on every collector deploy.
        poll_count           = GREATEST(endpoint_state.poll_count, EXCLUDED.poll_count),
        fail_count           = GREATEST(endpoint_state.fail_count, EXCLUDED.fail_count),
        timeout_count        = GREATEST(endpoint_state.timeout_count, EXCLUDED.timeout_count),
        auth_fail_count      = GREATEST(endpoint_state.auth_fail_count, EXCLUDED.auth_fail_count),
        updated_at           = now()
""")

# A refresh carries no status change, so device status cannot have moved. It
# still has to touch device_state.last_seen, or the device rolls up as stale
# for exactly the reason endpoint_state did.
_TOUCH_DEVICE_SEEN = text("""
    UPDATE device_state SET last_seen = GREATEST(device_state.last_seen, :last_seen),
                            updated_at = now()
     WHERE device_id = (SELECT device_id FROM device_endpoint
                         WHERE id = CAST(:endpoint_id AS uuid))
""")


async def apply_endpoint_state(session: AsyncSession, s: dict) -> None:
    """Persist communication state and, on a real transition, re-derive device status.

    Device status is the best of its endpoints: a server whose BMC is
    unreachable but whose OS agent answers is DEGRADED, not OFFLINE. Reporting
    it as OFFLINE would send an operator to a machine that is running fine.

    ``is_refresh`` marks a periodic liveness update rather than a transition.
    Those skip the rollup below: it is an aggregate over every endpoint of the
    device, and running it for all 1386 endpoints once a minute would be
    thousands of pointless aggregates an hour to recompute a status that by
    definition did not change.
    """
    await session.execute(_UPSERT_ENDPOINT_STATE, s)

    if s.get("is_refresh"):
        await session.execute(_TOUCH_DEVICE_SEEN,
                              {"endpoint_id": s["endpoint_id"],
                               "last_seen": s["last_seen"]})
        return
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
