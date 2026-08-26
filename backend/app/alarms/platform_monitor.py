"""Gathering the signals the platform evaluator judges, and persisting the result.

Split from ``platform.py`` so the rules stay testable without a database. This
half is the plumbing: read the state, hand it to ``evaluate``, write what came
back, and export the same numbers as Prometheus gauges.

The heartbeat deserves its own note. The ingest worker writes one to Redis on
every cycle carrying the pipeline lag it just measured, and the API reads it.
That is not duplication of the collector heartbeat - it is the answer to "who
watches the watcher". Pipeline lag can only be measured where the message is
consumed, and a worker that has died cannot report the fact, so the measurement
is written where a different process can find it.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.alarms import platform as rules
from app.core import metrics
from app.core.logging import get_logger

log = get_logger("platform")

HEARTBEAT_KEY = "dcim:ingest:heartbeat"
# Long enough that a slow cycle does not expire it, short enough that a stale
# key cannot outlive the incident it describes.
HEARTBEAT_TTL_S = 600


async def write_heartbeat(redis: Redis, *, consumer: str, lag_s: float | None,
                          batches: int, samples: int) -> None:
    """Record that the worker is alive, and how far behind it is."""
    payload = json.dumps({
        "at": time.time(), "consumer": consumer, "lag_s": lag_s,
        "batches": batches, "samples": samples,
    })
    await redis.set(HEARTBEAT_KEY, payload, ex=HEARTBEAT_TTL_S)


async def read_heartbeat(redis: Redis) -> dict[str, Any] | None:
    raw = await redis.get(HEARTBEAT_KEY)
    if not raw:
        return None
    try:
        hb = json.loads(raw)
    except ValueError:
        return None
    hb["age_s"] = max(0.0, time.time() - float(hb.get("at", 0)))
    return hb


async def _telemetry_freshness(session: AsyncSession) -> tuple[float | None, bool]:
    """Age of the newest sample, and whether any sample exists at all.

    Deliberately unbounded. Computed with a lookback window - which is how the
    dashboard and /ready did it - the answer becomes NULL once the outage is
    longer than the window, so the metric vanishes exactly when the pipeline
    dies. Every dashboard then reads "no data" and every threshold stops
    evaluating, at the moment they were needed.

    `present` is derived from the max, NOT from a count. It used to be
    ``count(*) > 0`` in this same statement, and on a 57-million-row hypertable
    with compressed chunks that is a full scan: 60 SECONDS, measured, against
    4.7 ms for the max alone. This runs inside the ingest worker's tick, so the
    monitor was stalling the pipeline it exists to watch - the worker spent most
    of every tick in here, ingest ran at roughly 60% of the rate the collector
    was producing, and the estate's data fell an hour behind while the platform
    reported itself healthy in between scans.

    The two forms mean the same thing anyway: a table with no rows has no
    maximum, and one with a maximum has rows.
    """
    row = (await session.execute(text("""
        SELECT extract(epoch FROM (now() - max(ts))) AS age_s
          FROM telemetry_sample
    """))).mappings().first()
    age = row["age_s"] if row is not None else None
    if age is None:
        return None, False
    return float(age), True


async def _collectors(session: AsyncSession) -> list[rules.Collector]:
    # clock_timestamp(), not now(): now() is the transaction start time, so a
    # heartbeat committed after this transaction began reads as being in the
    # future and the age comes back negative. It showed up live as a collector
    # "-5743 ms ago", which is nonsense on screen and, worse, sails under every
    # staleness threshold no matter how large the real skew is.
    rows = (await session.execute(text("""
        SELECT id, status, endpoints_owned, endpoints_online, stats,
               extract(epoch FROM (clock_timestamp() - last_heartbeat))
                   AS heartbeat_age_s
          FROM collector_instance
    """))).mappings().all()
    out = []
    for r in rows:
        stats = r["stats"] or {}
        if isinstance(stats, str):
            try:
                stats = json.loads(stats)
            except ValueError:
                stats = {}
        out.append(rules.Collector(
            collector_id=r["id"],
            # Clamped: a small negative age is clock skew between writer and
            # reader, not a heartbeat from the future.
            heartbeat_age_s=(max(0.0, float(r["heartbeat_age_s"]))
                             if r["heartbeat_age_s"] is not None else None),
            status=r["status"],
            endpoints_owned=int(r["endpoints_owned"] or 0),
            endpoints_online=int(r["endpoints_online"] or 0),
            publish_queue_depth=stats.get("queue_depth"),
            publish_queue_capacity=stats.get("queue_capacity"),
            publish_dropped=int(stats.get("publish_dropped") or 0),
        ))
    return out


async def _stream_pending(redis: Redis, streams: list[str],
                          group: str) -> dict[str, int]:
    """Delivered-but-unacknowledged entries per stream.

    The leading indicator: pending climbs before lag does, because an entry has
    to be delivered and left unacknowledged before anything is measurably late.
    A stream or group that does not exist yet is not an error - it is a
    platform that has not started consuming - and reports nothing rather than
    zero.
    """
    out: dict[str, int] = {}
    for name in streams:
        try:
            info = await redis.xpending(name, group)
        except Exception:
            continue
        if info:
            out[name] = int(info.get("pending", 0) if isinstance(info, dict)
                            else info[0])
    return out


async def gather(session: AsyncSession, redis: Redis, *,
                 streams: list[str], group: str,
                 poll_interval_s: float = 120.0,
                 ingest_lag_s: float | None = None,
                 collectors_expected: int = 1) -> rules.Signals:
    """Read every signal the evaluator needs, and export them as metrics.

    ``ingest_lag_s`` is passed in when the caller is the worker, which measured
    it directly. When the caller is the API it comes from the worker heartbeat,
    which is a second-hand reading of the same number and is treated as such.
    """
    age, present = await _telemetry_freshness(session)
    collectors = await _collectors(session)
    pending = await _stream_pending(redis, streams, group)
    hb = await read_heartbeat(redis)

    lag = ingest_lag_s
    if lag is None and hb is not None:
        lag = hb.get("lag_s")

    metrics.telemetry_seen.set(1 if present else 0)
    if age is not None:
        metrics.telemetry_age.set(age)
    if lag is not None:
        metrics.ingest_lag.labels(stream=streams[0] if streams else "telemetry.v1").set(lag)
    for name, depth in pending.items():
        metrics.ingest_stream_pending.labels(stream=name).set(depth)
    metrics.collectors_up.set(sum(
        1 for c in collectors
        if c.heartbeat_age_s is not None
        and c.heartbeat_age_s < rules.COLLECTOR_STALE_S))
    for c in collectors:
        if c.heartbeat_age_s is not None:
            metrics.collector_heartbeat_age.labels(
                collector_id=c.collector_id).set(c.heartbeat_age_s)

    return rules.Signals(
        ingest_lag_s=lag,
        telemetry_age_s=age,
        telemetry_present=present,
        worker_heartbeat_age_s=hb.get("age_s") if hb else None,
        poll_interval_s=poll_interval_s,
        collectors=collectors,
        collectors_expected=collectors_expected,
        stream_pending=pending,
    )


async def apply(session: AsyncSession, findings: list[rules.Finding]
                ) -> dict[str, Any]:
    """Raise what should be open, clear what should not, report what changed."""
    from app.repositories import alarms as repo

    now = datetime.now(UTC)
    open_rows = await repo.open_platform_alarms(session)
    open_keys = {(r["alarm_type"], r["instance"]) for r in open_rows}

    _, to_clear = rules.diff(findings, open_keys)

    raised = []
    for f in findings:
        row = await repo.raise_platform_alarm(
            session, alarm_type=f.alarm_type, instance=f.instance,
            severity=f.severity, message=f.message, observed_at=now,
            value=f.value, threshold=f.threshold)
        if row and row["change"] in ("created", "severity_changed"):
            raised.append(row)
            metrics.alarm_transitions.labels(action="raised").inc()
            log.warning("platform alarm", alarm_type=f.alarm_type,
                        instance=f.instance, severity=f.severity,
                        message=f.message)

    cleared = await repo.clear_platform_alarms(session, keys=to_clear, at=now)
    for row in cleared:
        metrics.alarm_transitions.labels(action="cleared").inc()
        log.info("platform alarm cleared", alarm_type=row["alarm_type"],
                 instance=row["instance"])

    counts = await repo.active_alarm_counts(session)
    # Reset first: a severity that has dropped to zero keeps its last value
    # forever otherwise, and a stale gauge reading CRITICAL=3 outlives the
    # incident.
    metrics.alarms_active.clear()
    for c in counts:
        metrics.alarms_active.labels(
            severity=c["severity"], origin=c["origin"]).set(c["n"])

    await session.commit()
    return {"raised": raised, "cleared": cleared,
            "open": len(findings), "summary": rules.summarise(findings)}


async def run_once(session: AsyncSession, redis: Redis, *, streams: list[str],
                   group: str, poll_interval_s: float = 120.0,
                   ingest_lag_s: float | None = None) -> dict[str, Any]:
    signals = await gather(session, redis, streams=streams, group=group,
                           poll_interval_s=poll_interval_s,
                           ingest_lag_s=ingest_lag_s)
    findings = rules.evaluate(signals)
    result = await apply(session, findings)
    result["signals"] = {
        "ingest_lag_s": signals.ingest_lag_s,
        "telemetry_age_s": signals.telemetry_age_s,
        "telemetry_present": signals.telemetry_present,
        "worker_heartbeat_age_s": signals.worker_heartbeat_age_s,
        "collectors": len(signals.collectors),
        "stream_pending": signals.stream_pending,
    }
    return result
