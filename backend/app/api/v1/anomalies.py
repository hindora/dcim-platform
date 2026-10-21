"""Anomaly detection API (docs/30). Phase 0: GET /anomalies/health only.

Viewer-readable, because the UI decides what to hide from it (docs/31 D6).
With DCIM_ANOMALY_ENABLED=false it still answers 200: `enabled: false`, the
configured `explainer`, and zero or null figures. Feature-off is not an error
(docs/30 section 1), and the database is not read at all in that case, so
the endpoint also answers on a database that has not reached migration 0111.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import sweep as anomaly_sweep
from app.analytics.catalogue import DERIVED_SEVERITY_CAP
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import Principal, require_role
from app.db.session import get_session
from app.repositories import anomalies as repo
from app.schemas.anomalies import (
    AnomalyHealth,
    BaselineDetail,
    FindingsDetail,
    HealthDetail,
    IncidentsDetail,
    SweepDetail,
)

log = get_logger("api.anomalies")

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


async def get_redis(settings: Settings = Depends(get_settings)) -> AsyncIterator[Redis]:
    """One client per request, as the other Redis-reading routes do."""
    redis = Redis.from_url(settings.redis_url)
    try:
        yield redis
    finally:
        await redis.aclose()


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return int(s[min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)])


def _parse(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def sweep_detail(outcomes: list[dict], heartbeat: dict | None) -> SweepDetail:
    ran = [e for e in outcomes if e.get("outcome") == "ran"]
    errors = [e for e in outcomes if e.get("outcome") == "error"]
    last_error = (heartbeat or {}).get("error")
    if not last_error and errors:
        last_error = errors[0].get("error")
    return SweepDetail(
        p95_ms_1h=_p95([int(e["ms"]) for e in ran if "ms" in e]),
        runs_1h=len(ran),
        gate_skips_1h=sum(1 for e in outcomes if e.get("outcome") == "cadence"),
        errors_1h=len(errors), last_error=last_error,
        series_scored=int((heartbeat or {}).get("series_scored") or 0))


@router.get("/health", response_model=AnomalyHealth,
            summary="Is the anomaly sweep running, and how is it doing")
async def health(session: AsyncSession = Depends(get_session),
                 redis: Redis = Depends(get_redis),
                 settings: Settings = Depends(get_settings),
                 _: Principal = Depends(require_role("viewer"))) -> AnomalyHealth:
    base = {"enabled": settings.anomaly_enabled, "explainer": settings.anomaly_explainer,
            "sweep_s": settings.anomaly_sweep_s, "severity_cap": DERIVED_SEVERITY_CAP}
    if not settings.anomaly_enabled:
        return AnomalyHealth(**base)

    now = datetime.now(UTC)
    # Redis first and tolerant: a Redis outage must not 500 the health view,
    # it is exactly when somebody opens it.
    hb = baseline_hb = None
    outcomes: list[dict] = []
    try:
        hb = await anomaly_sweep.read_heartbeat(redis)
        baseline_hb = await anomaly_sweep.read_heartbeat(
            redis, anomaly_sweep.BASELINE_HEARTBEAT_KEY)
        outcomes = await anomaly_sweep.read_outcomes(redis, now)
    except Exception as exc:
        log.warning("anomaly health could not read redis", error=str(exc))

    counts = await repo.health_counts(session)

    baselines = BaselineDetail()
    if baseline_hb:
        start, end = _parse(baseline_hb.get("started_at")), _parse(baseline_hb.get("finished_at"))
        baselines = BaselineDetail(
            last_fit_duration_s=(end - start).total_seconds() if start and end else None,
            series_fitted=int(baseline_hb.get("series_fitted") or 0),
            rejected=int(baseline_hb.get("rejected") or 0))

    return AnomalyHealth(
        **base,
        last_sweep_at=_parse((hb or {}).get("finished_at")),
        last_sweep_ms=(hb or {}).get("duration_ms"),
        findings_per_hour=counts["findings_per_hour"],
        budget_per_hour=None,
        lock_holder=(hb or {}).get("consumer"),
        baseline_last_fit_at=counts["baseline_last_fit_at"],
        l5_major_enabled=counts["l5_major_enabled"],
        detail=HealthDetail(
            sweep=sweep_detail(outcomes, hb),
            baselines=baselines,
            findings=FindingsDetail(**counts["findings"]),
            incidents=IncidentsDetail(**counts["incidents"])))
