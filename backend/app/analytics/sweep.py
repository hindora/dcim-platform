"""The anomaly sweep, and the guards that make exactly one of it run.

Phase 0 ships the plumbing with nothing to score: `run_sweep` loads the
catalogue and returns. What is real already is everything around it, because
that is what has to be measured on the live host before a detector is added
(docs/33 task 0.16):

* **One sweep per interval, across replicas.** The two ingest replicas run
  unaligned monotonic timers. The lock holder is decided by
  `pg_try_advisory_xact_lock(ANOMALY_SWEEP_LOCK_KEY)` inside the sweep's own
  transaction, as the assigner does, and released at commit. The lock alone
  would still admit replica B sweeping 30 s after A, doubling the work and
  halving every dwell, so the holder then reads the `dcim:anomaly:sweep`
  heartbeat and skips when its `finished_at` is younger than 0.9 x
  DCIM_ANOMALY_SWEEP_S - the **cadence gate** (docs/29 section 10.1). A
  missing key means sweep: the lock still serialises.

* **The heartbeat is written while the lock is held**, before the caller
  commits. Written after commit, there is a window in which the other
  replica can take the released lock and read the previous, older
  heartbeat, and sweep a second time.

* **The nightly baseline fit** takes a SESSION-level
  `pg_try_advisory_lock(ANOMALY_BASELINE_LOCK_KEY)` on a dedicated
  connection, released in `finally`, because the fit spans many short
  per-batch transactions and one transaction held for minutes would pin the
  vacuum horizon on a host that already swaps. The once-per-UTC-day guard is
  `max(anomaly_baseline.fitted_at)`, in Postgres, so a Redis flush cannot
  trigger a second full fit.

Lock constants live in code, not env, beside the existing ones: assigner
"DCIM5", rollout "DCIM7", collector visibility "colv".
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.analytics.catalogue import (
    DERIVED_SEVERITY_CAP,  # noqa: F401 - the cap lives with the sweep
)
from app.core.logging import get_logger
from app.repositories import anomalies as repo

log = get_logger("anomaly")

#: "DCIMA" - the per-sweep transaction lock.
ANOMALY_SWEEP_LOCK_KEY = 0x44_43_49_4D_41
#: "DCIMB" - the nightly fit's session lock.
ANOMALY_BASELINE_LOCK_KEY = 0x44_43_49_4D_42

SWEEP_HEARTBEAT_KEY = "dcim:anomaly:sweep"
BASELINE_HEARTBEAT_KEY = "dcim:anomaly:baseline"
#: Not in docs/29 section 10's key table: the rolling record /health needs
#: for runs_1h, gate_skips_1h, errors_1h and p95_ms_1h (docs/30 section 11).
#: The heartbeat holds only the last sweep, and Prometheus counters are per
#: process, so neither can answer "how many sweeps ran in the last hour across
#: both replicas". One capped list, one compact entry per timer firing.
SWEEP_LOG_KEY = "dcim:anomaly:sweep:log"
SWEEP_LOG_MAX = 512
SWEEP_LOG_TTL_S = 7200

#: Skip when the last sweep finished less than this fraction of the cadence ago.
CADENCE_GATE_FRACTION = 0.9
#: The heartbeat outlives ten intervals, then /health reports "no sweep".
HEARTBEAT_TTL_SWEEPS = 10
BASELINE_HEARTBEAT_TTL_S = 48 * 3600

# Module tunables per docs/29 section 11 (in code, not env).
ACC_TTL_S = 86_400
PRUNE_BATCH = 5_000
BUNDLE_CACHE_TTL_S = 600


@dataclass
class SweepResult:
    ran: bool
    #: Why it did not run: 'lock_held' | 'cadence'.
    skipped: str | None = None
    series_scored: int = 0
    findings_opened: int = 0
    findings_resolved: int = 0
    alerts_raised: int = 0
    duration_ms: int = 0
    #: Alarm actions for the integration outbox and fanout (Phase 1).
    actions: list[Any] = field(default_factory=list)
    #: Frames for the `anomalies` websocket topic, at most 50 (Phase 1).
    frames: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FitResult:
    ran: bool
    skipped: str | None = None
    series_fitted: int = 0
    rejected: int = 0
    pruned_rows: int = 0


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --- the sweep ---------------------------------------------------------------

async def run_sweep(session: AsyncSession, now: datetime) -> SweepResult:
    """One pass over every enabled detector. Phase 0: no detectors.

    Loads the catalogue so the empty-sweep timing measured in Phase 0
    includes the one read every later sweep pays, and scores nothing.
    Phase 1 adds the reader, L0, L2 and the raise/clear path here; any
    analytics raise goes through catalogue.cap_severity first.
    """
    await repo.load_catalogue(session, now)
    return SweepResult(ran=True)


async def try_sweep_lock(session: AsyncSession) -> bool:
    return bool((await session.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": ANOMALY_SWEEP_LOCK_KEY})).scalar())


async def read_heartbeat(redis: Any, key: str = SWEEP_HEARTBEAT_KEY) -> dict[str, Any] | None:
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        hb = json.loads(raw)
    except ValueError:
        return None
    return hb if isinstance(hb, dict) else None


def cadence_gate_closed(heartbeat: dict[str, Any] | None, now: datetime,
                        sweep_s: int) -> bool:
    """True when the last sweep finished under 0.9 x sweep_s ago.

    A heartbeat from the future (clock skew between replicas) also closes the
    gate: skipping one interval is cheaper than sweeping twice.
    """
    if not heartbeat:
        return False
    finished = _parse(heartbeat.get("finished_at"))
    if finished is None:
        return False
    return (now - finished).total_seconds() < CADENCE_GATE_FRACTION * sweep_s


async def write_heartbeat(redis: Any, *, consumer: str, started_at: datetime,
                          finished_at: datetime, result: SweepResult,
                          sweep_s: int, error: str | None = None) -> None:
    payload = {
        "consumer": consumer, "started_at": _iso(started_at),
        "finished_at": _iso(finished_at), "duration_ms": result.duration_ms,
        "series_scored": result.series_scored,
        "findings_opened": result.findings_opened,
        "findings_resolved": result.findings_resolved,
        "alerts_raised": result.alerts_raised, "error": error,
    }
    await redis.set(SWEEP_HEARTBEAT_KEY, json.dumps(payload),
                    ex=HEARTBEAT_TTL_SWEEPS * sweep_s)


async def record_outcome(redis: Any, *, consumer: str, outcome: str, at: datetime,
                         duration_ms: int | None = None, error: str | None = None) -> None:
    """Append to the rolling sweep log /health reads. Best effort."""
    entry = {"at": at.timestamp(), "consumer": consumer, "outcome": outcome}
    if duration_ms is not None:
        entry["ms"] = duration_ms
    if error:
        entry["error"] = error[:200]
    try:
        await redis.lpush(SWEEP_LOG_KEY, json.dumps(entry))
        await redis.ltrim(SWEEP_LOG_KEY, 0, SWEEP_LOG_MAX - 1)
        await redis.expire(SWEEP_LOG_KEY, SWEEP_LOG_TTL_S)
    except Exception as exc:  # the sweep's outcome does not depend on its log
        log.warning("anomaly sweep log write failed", error=str(exc))


async def read_outcomes(redis: Any, now: datetime, window_s: int = 3600) -> list[dict]:
    raw = await redis.lrange(SWEEP_LOG_KEY, 0, SWEEP_LOG_MAX - 1)
    cutoff = now.timestamp() - window_s
    out = []
    for item in raw or ():
        try:
            e = json.loads(item)
        except (TypeError, ValueError):
            continue
        if isinstance(e, dict) and float(e.get("at", 0)) >= cutoff:
            out.append(e)
    return out


SweepFn = Callable[[AsyncSession, datetime], Awaitable[SweepResult]]


async def guarded_sweep(session: AsyncSession, redis: Any, *, consumer: str,
                        sweep_s: int, now: datetime | None = None,
                        sweep: SweepFn | None = None) -> SweepResult:
    """Lock, gate, sweep, heartbeat - in the caller's transaction.

    The caller owns the transaction (`unit_of_work()`): commit releases the
    lock, and the outbox rows the sweep enqueues commit with its writes.
    """
    now = now or datetime.now(UTC)
    if not await try_sweep_lock(session):
        return SweepResult(ran=False, skipped="lock_held")
    if cadence_gate_closed(await read_heartbeat(redis), now, sweep_s):
        return SweepResult(ran=False, skipped="cadence")
    t0 = time.perf_counter()
    result = await (sweep or run_sweep)(session, now)
    result.duration_ms = int((time.perf_counter() - t0) * 1000)
    # `now` is the sweep's logical time; finished_at must be comparable with
    # the next caller's `now`, so it is advanced by the measured duration
    # rather than read from a second clock.
    finished = now + timedelta(milliseconds=result.duration_ms)
    await write_heartbeat(redis, consumer=consumer, started_at=now, finished_at=finished,
                          result=result, sweep_s=sweep_s)
    return result


# --- the nightly fit ---------------------------------------------------------

async def run_baseline_fit(conn: AsyncConnection, now: datetime) -> FitResult:
    """Fit every L3/L4 series, then prune. Phase 0: nothing to fit.

    Phase 3 fills this with per-batch transactions on `conn`; the prune
    (docs/29 section 6.3) arrives with task 1.15.
    """
    return FitResult(ran=True)


FitFn = Callable[[AsyncConnection, datetime], Awaitable[FitResult]]


async def guarded_fit(conn: AsyncConnection, redis: Any, *, consumer: str,
                      hour_utc: int, now: datetime | None = None,
                      fit: FitFn | None = None) -> FitResult:
    """Session lock, daily guard, fit, unlock - on a dedicated connection.

    `conn` must not be shared with anything else: a session-level advisory
    lock belongs to the connection, and a pooled connection handed back with
    the lock still held would make the next borrower the holder.
    """
    now = now or datetime.now(UTC)
    got = bool((await conn.execute(text("SELECT pg_try_advisory_lock(:key)"),
                                   {"key": ANOMALY_BASELINE_LOCK_KEY})).scalar())
    # Close the implicit transaction at once: the lock is session-level and
    # survives it, and an idle-in-transaction connection pins the horizon.
    await conn.commit()
    if not got:
        return FitResult(ran=False, skipped="lock_held")
    try:
        since = repo.fit_day_start(now, hour_utc)
        last = (await conn.execute(text("SELECT max(fitted_at) FROM anomaly_baseline"))).scalar()
        await conn.commit()
        if last is not None and last >= since:
            return FitResult(ran=False, skipped="already_fitted")
        # Secondary, Redis-side: a pass that fitted nothing (every Phase 0
        # pass, and a Phase 3 night with no eligible series) writes no
        # baseline row, so Postgres alone would re-run it on every check.
        # Losing this key only costs one repeat of that empty pass.
        hb = await read_heartbeat(redis, BASELINE_HEARTBEAT_KEY)
        if hb and hb.get("fit_date") == since.date().isoformat():
            return FitResult(ran=False, skipped="already_fitted")
        started = datetime.now(UTC)
        result = await (fit or run_baseline_fit)(conn, now)
        finished = datetime.now(UTC)
        payload = {
            "consumer": consumer, "fit_date": since.date().isoformat(),
            "started_at": _iso(started), "finished_at": _iso(finished),
            "series_fitted": result.series_fitted, "rejected": result.rejected,
            "pruned_rows": result.pruned_rows,
        }
        await redis.set(BASELINE_HEARTBEAT_KEY, json.dumps(payload),
                        ex=BASELINE_HEARTBEAT_TTL_S)
        return result
    finally:
        try:
            await conn.rollback()
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"),
                               {"key": ANOMALY_BASELINE_LOCK_KEY})
            await conn.commit()
        except Exception as exc:
            # Closing the connection (the caller's context manager) releases a
            # session lock regardless; log and let that happen.
            log.warning("baseline lock release failed", error=str(exc))
