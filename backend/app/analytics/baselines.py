"""The nightly step: baseline fits (Phase 3) and the retention prune.

None of the anomaly tables is a hypertable, so retention is not a Timescale
policy but this prune (docs/29 section 6.3), run after the fit under the
DCIMB session lock. Each table is pruned in batches of PRUNE_BATCH rows, one
short transaction per batch, so no transaction holds a snapshot open for
long on a host that already swaps under Timescale jobs.

| table                      | deleted when                                       |
| anomaly_finding            | resolved more than 2 years ago (alarm_history's 2y)|
| anomaly_incident           | resolved more than 2 years ago                     |
| anomaly_baseline           | superseded more than 30 days ago                   |
| anomaly_state              | not updated for 7 days (the series stopped)        |
| anomaly_baseline_exclusion | ended more than 2 years ago                        |
| anomaly_override           | expired more than 7 days ago                       |

Incidents are pruned after findings: a finding's incident FK is SET NULL,
and its own two-year clock is the same, so the order only saves updates.
"""

from __future__ import annotations

import math as _math
import statistics as _stats
from dataclasses import dataclass as _dataclass
from datetime import UTC as _UTC
from datetime import datetime
from datetime import timedelta as _timedelta
from typing import Any

from sqlalchemy import text

from app.core.logging import get_logger
from app.repositories import anomalies as repo

log = get_logger("anomaly.baselines")

PRUNE_BATCH = 5_000
#: A runaway table must not keep the nightly step busy forever.
MAX_BATCHES_PER_TABLE = 200


async def prune(conn: Any, now: datetime, *, batch: int = PRUNE_BATCH) -> dict[str, int]:
    """Batched retention prune on a dedicated connection. Returns rows per table."""
    out: dict[str, int] = {}
    for table, key, predicate in repo.PRUNE_RULES:
        total = 0
        for _ in range(MAX_BATCHES_PER_TABLE):
            n = await repo.prune_batch(conn, table, key, predicate, now=now, batch=batch)
            await conn.commit()
            total += n
            if n < batch:
                break
        out[table] = total
    if any(out.values()):
        log.info("anomaly prune", **out)
    return out


# =============================================================================
# Phase 3a: the profile fit (docs/35 sections 3, 4 and 6)
# =============================================================================
#
# One series at a time: its hourly means from telemetry_1h, cleaned of what
# must not be learned, summarised per UTC hour of day as median and a robust
# spread. Pure (fit_profile) so the guards are tested without a database; the
# database pass (fit_profiles) only reads, calls it, and writes the rows.

#: How far back a fit reads. The raw retention is 30 days and 1 h is kept
#: forever, so this is a choice, not a limit: long enough for the part-time
#: estate to reach its minimum, short enough to follow a slow change.
FIT_LOOKBACK_DAYS = 30
#: MAD to sigma for a normal distribution.
MAD_TO_SIGMA = 1.4826


@_dataclass(frozen=True)
class HourRow:
    bucket: datetime        # the hour, UTC
    value: float            # its mean
    samples: int            # how many raw samples went into it


@_dataclass
class ProfileFit:
    status: str             # 'ok' | 'thin'
    model: dict[str, Any]
    n: int                  # hours that entered the fit
    days: int
    excluded: int           # hours dropped by a guard
    resid_sigma: float | None


def fit_profile(rows: list[HourRow], params: dict[str, Any], *,
                excluded_spans: list[tuple[datetime, datetime]] = (),
                previous: dict[str, Any] | None = None) -> ProfileFit:
    """Hour-of-day median and robust spread of one series, with the guards.

    Dropped before the fit:
      * thin hours - fewer samples than half the series' typical hour;
      * cold-start hours - the first hour after a gap of more than one hour
        (on this estate, every morning);
      * hours inside an excluded span (maintenance, a regime change, a
        confirmed finding's time).
    Then per hour of day: median, sigma = 1.4826 * MAD floored by
    `sigma_floor`, n. A previous fit limits how far each hour's median may
    move in one night (`max_change_pct`, or one previous sigma if larger), so
    one strange day cannot rewrite normal.

    Status 'ok' needs `min_hours` hours over at least `min_days` distinct days;
    otherwise 'thin' - still written, so the panel can say how far along the
    learning is.
    """
    rows = sorted(rows, key=lambda r: r.bucket)
    floor_sigma = float(params.get("sigma_floor", 0.0))
    if not rows:
        return ProfileFit("thin", {"hod": {}, "hours": 0, "days": 0}, 0, 0, 0, None)
    typical = _stats.median(r.samples for r in rows)
    keep: list[HourRow] = []
    dropped = 0
    prev_bucket: datetime | None = None
    for r in rows:
        cold = prev_bucket is None or r.bucket - prev_bucket > _timedelta(hours=1)
        prev_bucket = r.bucket
        thin = r.samples < 0.5 * typical
        inside = any(a <= r.bucket < b for a, b in excluded_spans)
        if thin or inside or (cold and keep_cold_start(params)) or not _math.isfinite(r.value):
            dropped += 1
            continue
        keep.append(r)

    by_hour: dict[int, list[float]] = {}
    for r in keep:
        by_hour.setdefault(r.bucket.astimezone(_UTC).hour, []).append(r.value)
    prev_hod = (previous or {}).get("hod") or {}
    cap = float(params.get("max_change_pct", 10.0)) / 100.0
    hod: dict[str, list[float]] = {}
    spreads: list[float] = []
    for hour, vals in sorted(by_hour.items()):
        med = _stats.median(vals)
        mad = _stats.median(abs(v - med) for v in vals)
        sigma = max(MAD_TO_SIGMA * mad, floor_sigma)
        old = prev_hod.get(str(hour))
        if old and int(old[2]) >= int(params.get("min_hour_samples", 8)):
            step = max(abs(float(old[0])) * cap, float(old[1]))
            med = min(max(med, float(old[0]) - step), float(old[0]) + step)
        hod[str(hour)] = [round(med, 4), round(sigma, 4), len(vals)]
        spreads.append(sigma)

    days = len({r.bucket.astimezone(_UTC).date() for r in keep})
    ok = len(keep) >= int(params.get("min_hours", 120)) and days >= int(params.get("min_days", 10))
    model = {"hod": hod, "hours": len(keep), "days": days,
             "min_hours": int(params.get("min_hours", 120))}
    return ProfileFit("ok" if ok else "thin", model, len(keep), days, dropped,
                      round(_stats.median(spreads), 4) if spreads else None)


def keep_cold_start(params: dict[str, Any]) -> bool:
    """Whether the first hour after a gap is excluded (docs/35 section 3)."""
    return bool(params.get("exclude_cold_start", True))


async def enabled_profile_detectors(conn: Any) -> list[dict[str, Any]]:
    from app.analytics.detectors.residual import RULES
    rows = (await conn.execute(text("""
        SELECT id, version, primary_metric, device_types, params FROM anomaly_detector
         WHERE layer = 3 AND enabled ORDER BY id
    """))).mappings().all()
    await conn.commit()
    return [dict(r) for r in rows if r["id"] in RULES]


async def fit_profiles(conn: Any, now: datetime, detectors: list[dict[str, Any]]
                       ) -> tuple[int, int]:
    """Fit every series of every enabled L3 profile detector. (fitted, thin).

    One detector at a time, one transaction per batch of series. A series
    with an open finding of that detector is not refitted: the baseline would
    learn the fault and the finding would resolve itself.
    """
    fitted = thin = 0
    since = now - _timedelta(days=FIT_LOOKBACK_DAYS)
    for det in detectors:
        params = dict(det["params"] or {})
        series = await repo.fit_series(conn, metric_key=det["primary_metric"],
                                       device_types=list(det["device_types"]),
                                       since=since, until=now)
        frozen = await repo.series_with_open_findings(conn, det["id"])
        current = await repo.current_baseline_models(conn, det["id"])
        spans = await repo.excluded_spans(conn, det["id"], since, now)
        await conn.commit()
        batch: list[dict[str, Any]] = []
        for (device_id, metric_id, instance), raw in series.items():
            if (device_id, metric_id, instance) in frozen:
                continue
            rows = [HourRow(*t) for t in raw]
            fit = fit_profile(rows, params,
                              excluded_spans=spans.get(device_id, []) + spans.get("*", []),
                              previous=current.get((device_id, metric_id, instance)))
            batch.append({
                "detector_id": det["id"], "detector_version": det["version"],
                "device_id": device_id, "metric_id": metric_id, "instance": instance,
                "model": fit.model, "fit_from": rows[0].bucket if rows else since,
                "fit_to": (rows[-1].bucket + _timedelta(hours=1)) if rows else now,
                "n": fit.n, "excluded_s": fit.excluded * 3600, "r2": None,
                "resid_sigma": fit.resid_sigma, "status": fit.status, "fitted_at": now,
            })
            fitted += fit.status == "ok"
            thin += fit.status == "thin"
            if len(batch) >= 200:
                await repo.write_baselines(conn, batch)
                await conn.commit()
                batch = []
        if batch:
            await repo.write_baselines(conn, batch)
            await conn.commit()
        log.info("anomaly profiles fitted", detector=det["id"], series=len(series),
                 frozen=len(frozen))
    return fitted, thin
