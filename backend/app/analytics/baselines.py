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
    floor_sigma = float(params.get("sigma_floor", 0.0))
    if not rows:
        return ProfileFit("thin", {"hod": {}, "hours": 0, "days": 0}, 0, 0, 0, None)
    keep, dropped = _clean_rows(rows, params, list(excluded_spans))

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


@_dataclass(frozen=True)
class PairRow:
    bucket: datetime
    y: float
    #: One value per driver, already transformed. A bare float is one driver.
    x: Any
    samples: int

    @property
    def xs(self) -> tuple[float, ...]:
        return tuple(self.x) if isinstance(self.x, (tuple, list)) else (float(self.x),)


def _clean_rows(rows: list, params: dict[str, Any],
                excluded_spans: list[tuple[datetime, datetime]]) -> tuple[list, int]:
    """The guards shared by every fit: thin hours, cold starts, exclusions."""
    rows = sorted(rows, key=lambda r: r.bucket)
    if not rows:
        return [], 0
    typical = _stats.median(r.samples for r in rows)
    keep, dropped, prev = [], 0, None
    for r in rows:
        cold = prev is None or r.bucket - prev > _timedelta(hours=1)
        prev = r.bucket
        vals = (r.y, *r.xs) if isinstance(r, PairRow) else (r.value,)
        bad = (r.samples < 0.5 * typical
               or any(a <= r.bucket < b for a, b in excluded_spans)
               or (cold and keep_cold_start(params))
               or not all(_math.isfinite(v) for v in vals))
        if bad:
            dropped += 1
        else:
            keep.append(r)
    return keep, dropped


def _solve(m: list[list[float]], v: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting; None if singular."""
    n = len(v)
    a = [[*row, v[k]] for k, row in enumerate(m)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(a[r][c]))
        if abs(a[piv][c]) < 1e-12:
            return None
        a[c], a[piv] = a[piv], a[c]
        for r in range(n):
            if r != c:
                f = a[r][c] / a[c][c]
                for k in range(c, n + 1):
                    a[r][k] -= f * a[c][k]
    return [a[k][n] / a[k][k] for k in range(n)]


def fit_ols(rows: list[PairRow], params: dict[str, Any], *,
            excluded_spans: list[tuple[datetime, datetime]] = (),
            previous: dict[str, Any] | None = None,
            drivers: tuple[str, ...] = ()) -> ProfileFit:
    """y = a + sum(b_i * x_i) on the hourly means, with the shared guards.

    Least squares on one or more drivers (docs/35 section 4). The residual
    spread is robust (1.4826 * MAD) and floored. Each driver's learned range
    (5th to 95th percentile) is kept, so scoring never extrapolates. A link
    weaker than `min_r2` is not a baseline: the status stays 'thin' with the
    reason in the model. One night can move the prediction at the middle of
    the data by at most `max_change_pct` (or one previous sigma).
    """
    keep, dropped = _clean_rows(rows, params, list(excluded_spans))
    days = len({r.bucket.astimezone(_UTC).date() for r in keep})
    base = {"hours": len(keep), "days": days, "min_hours": int(params.get("min_hours", 120)),
            "drivers": list(drivers)}
    k = len(keep[0].xs) if keep else max(1, len(drivers))
    if len(keep) < k + 2:
        return ProfileFit("thin", {**base, "reason": "too_little_data"}, len(keep), days,
                          dropped, None)
    cols = [[r.xs[i] for r in keep] for i in range(k)]
    ys = [r.y for r in keep]
    if any(max(c) - min(c) <= 1e-9 for c in cols):
        return ProfileFit("thin", {**base, "reason": "driver_does_not_vary"}, len(keep), days,
                          dropped, None)
    # Normal equations on [1, x_1 .. x_k].
    design = [[1.0, *r.xs] for r in keep]
    xtx = [[sum(row[a] * row[b] for row in design) for b in range(k + 1)] for a in range(k + 1)]
    xty = [sum(row[a] * y for row, y in zip(design, ys, strict=True)) for a in range(k + 1)]
    beta = _solve(xtx, xty)
    if beta is None:
        return ProfileFit("thin", {**base, "reason": "drivers_collinear"}, len(keep), days,
                          dropped, None)
    a, bs = beta[0], beta[1:]
    pred = [a + sum(b * x for b, x in zip(bs, r.xs, strict=True)) for r in keep]
    resid = [y - f for y, f in zip(ys, pred, strict=True)]
    ybar = _stats.fmean(ys)
    ss_res = sum(e * e for e in resid)
    ss_tot = sum((y - ybar) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    med = _stats.median(resid)
    sigma = max(MAD_TO_SIGMA * _stats.median(abs(e - med) for e in resid),
                float(params.get("sigma_floor", 0.0)))
    xbar = [_stats.fmean(c) for c in cols]
    lo, hi = [], []
    for c in cols:
        q = sorted(c)
        lo.append(q[int(0.05 * (len(q) - 1))])
        hi.append(q[int(0.95 * (len(q) - 1))])
    if previous and previous.get("a") is not None:
        pb = previous.get("b")
        pb = pb if isinstance(pb, list) else [pb]
        if len(pb) == k:
            cap = float(params.get("max_change_pct", 10.0)) / 100.0
            old_mid = float(previous["a"]) + sum(float(b) * x for b, x in zip(pb, xbar,
                                                                            strict=True))
            new_mid = a + sum(b * x for b, x in zip(bs, xbar, strict=True))
            step = max(abs(old_mid) * cap, float(previous.get("sigma") or 0.0))
            if abs(new_mid - old_mid) > step:
                a += (old_mid + step * (1 if new_mid > old_mid else -1)) - new_mid
    model = {**base, "a": round(a, 6), "b": [round(b, 6) for b in bs],
             "sigma": round(sigma, 4), "r2": round(r2, 4),
             "x_lo": [round(v, 4) for v in lo], "x_hi": [round(v, 4) for v in hi],
             "xbar": [round(v, 4) for v in xbar]}
    enough = (len(keep) >= int(params.get("min_hours", 120))
              and days >= int(params.get("min_days", 10)))
    weak = r2 < float(params.get("min_r2", 0.3))
    if weak:
        model["reason"] = "weak_link"
    return ProfileFit("ok" if enough and not weak else "thin", model, len(keep), days,
                      dropped, round(sigma, 4))


def keep_cold_start(params: dict[str, Any]) -> bool:
    """Whether the first hour after a gap is excluded (docs/35 section 3)."""
    return bool(params.get("exclude_cold_start", True))


async def _ols_series(conn: Any, spec: Any, device_types: list[str], since: datetime,
                      until: datetime) -> dict[tuple[str, int, str], list[tuple]]:
    """y joined with each driver on the same device and hour, transformed.

    Joined here rather than in SQL: each series is one indexed read, and a
    driver with several instances (four fans, two PSUs) is averaged per hour.
    """
    from app.analytics.l3_specs import transform
    ys = await repo.fit_series(conn, metric_key=spec.y, device_types=device_types,
                               since=since, until=until)
    drv: list[dict[tuple[str, datetime], float]] = []
    for d in spec.drivers:
        raw = await repo.fit_series(conn, metric_key=d.key, device_types=device_types,
                                    since=since, until=until)
        acc: dict[tuple[str, datetime], list[float]] = {}
        for (dev, _mid, _inst), rows in raw.items():
            for bucket, value, _n in rows:
                acc.setdefault((dev, bucket), []).append(value)
        drv.append({k: transform(sum(v) / len(v), d.transform) for k, v in acc.items()})
    out: dict[tuple[str, int, str], list[tuple]] = {}
    for key, rows in ys.items():
        dev = key[0]
        joined = []
        for bucket, value, n in rows:
            xs = [m.get((dev, bucket)) for m in drv]
            if all(x is not None for x in xs):
                joined.append((bucket, value, tuple(xs), n))
        if joined:
            out[key] = joined
    return out


async def enabled_profile_detectors(conn: Any) -> list[dict[str, Any]]:
    from app.analytics.l3_specs import SPECS as RULES
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
        from app.analytics.l3_specs import SPECS
        spec = SPECS.get(det["id"])
        kind = spec.kind if spec else "profile"
        types = list(det["device_types"])
        if kind == "ols":
            series = await _ols_series(conn, spec, types, since, now)
        elif kind == "gap":
            series = await repo.fit_room_gap(conn, y_key=spec.y, ref_key=spec.ref,
                                             ref_types=list(spec.ref_types),
                                             device_types=types, since=since, until=now)
        else:
            series = await repo.fit_series(conn, metric_key=spec.y if spec else
                                           det["primary_metric"], device_types=types,
                                           since=since, until=now)
        frozen = await repo.series_with_open_findings(conn, det["id"])
        current = await repo.current_baseline_models(conn, det["id"])
        spans = await repo.excluded_spans(conn, det["id"], since, now)
        await conn.commit()
        batch: list[dict[str, Any]] = []
        for (device_id, metric_id, instance), raw in series.items():
            if (device_id, metric_id, instance) in frozen:
                continue
            excl = spans.get(device_id, []) + spans.get("*", [])
            prev = current.get((device_id, metric_id, instance))
            if kind == "ols":
                rows = [PairRow(*t) for t in raw]
                fit = fit_ols(rows, params, excluded_spans=excl, previous=prev,
                              drivers=tuple(d.key for d in spec.drivers))
            else:
                rows = [HourRow(*t) for t in raw]
                fit = fit_profile(rows, params, excluded_spans=excl, previous=prev)
                if kind == "gap":
                    fit.model["of"] = spec.of
            batch.append({
                "detector_id": det["id"], "detector_version": det["version"],
                "device_id": device_id, "metric_id": metric_id, "instance": instance,
                "model_kind": "ols" if kind == "ols" else "profile_hod",
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
