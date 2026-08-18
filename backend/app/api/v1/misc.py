"""Dashboard, auth, metric registry and health endpoints."""

from __future__ import annotations

import hmac
import os
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.metrics_gen import METRICS
from app.core.security import Principal, current_principal, issue_token
from app.db.session import get_session
from app.schemas import DashboardSummary, LoginRequest, TokenResponse
from app.services import dashboard as service

router = APIRouter(tags=["platform"])

# Phase 1 has no user table. Credentials come from the environment so that no
# password is ever committed, and the login path is a single place to replace
# with a real user store in phase 4.
_DEFAULT_USER = os.environ.get("DCIM_ADMIN_USER", "admin")


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for a JWT")
async def login(req: LoginRequest,
                settings: Settings = Depends(get_settings)) -> TokenResponse:
    expected = os.environ.get("DCIM_ADMIN_PASSWORD")
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "DCIM_ADMIN_PASSWORD is not configured")
    if req.username != _DEFAULT_USER or not hmac.compare_digest(req.password, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")
    return TokenResponse(
        token=issue_token(req.username, "admin", settings),
        expires_in=settings.jwt_ttl_minutes * 60,
        username=req.username, role="admin",
    )


@router.get("/dashboard/summary", response_model=DashboardSummary,
            summary="Everything the dashboard needs, in one request")
async def dashboard_summary(
    datacenter_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> DashboardSummary:
    return await service.summary(session, datacenter_id)


@router.get("/metrics/registry", summary="The canonical metric dictionary")
async def metric_registry(_: Principal = Depends(current_principal)) -> dict:
    return {"items": [
        {"key": m.key, "display_name": m.display_name, "unit": m.unit,
         "value_type": m.value_type, "aggregation": m.aggregation,
         "min_valid": m.min_valid, "max_valid": m.max_valid,
         "stale_after_s": m.stale_after_s, "hot": m.hot, "group": m.group}
        for m in METRICS.values()
    ]}


@router.get("/health", summary="Liveness")
async def health() -> dict:
    return {"status": "ok", "time": datetime.now(UTC)}


@router.get("/ready", summary="Readiness: database, Redis and ingest lag")
async def ready(response: Response,
                session: AsyncSession = Depends(get_session),
                settings: Settings = Depends(get_settings)) -> dict:
    checks: dict[str, object] = {}
    ok = True

    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        ok = False

    redis = Redis.from_url(settings.redis_url)
    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"
        ok = False
    finally:
        await redis.aclose()

    # A backend that is up but far behind on ingest is not ready, and nothing
    # else in the system will say so.
    try:
        lag = (await session.execute(text("""
            SELECT extract(epoch FROM (now() - max(ts)))
            FROM telemetry_sample WHERE ts > now() - interval '1 hour'
        """))).scalar()
        checks["ingest_lag_seconds"] = float(lag) if lag is not None else None
        if lag is not None and float(lag) > 300:
            checks["ingest"] = "lagging"
            ok = False
    except Exception as exc:
        checks["ingest_lag_seconds"] = f"error: {exc}"

    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": ok, "checks": checks}
