"""Dashboard, auth, metric registry and health endpoints."""

from __future__ import annotations

import contextlib
import hmac
import os
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.alarms import platform as platform_rules
from app.alarms import platform_monitor
from app.core import audit
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.metrics_gen import METRICS
from app.core.security import Principal, current_principal, issue_token, require_role
from app.db.session import get_session
from app.schemas import DashboardSummary, LoginRequest, TokenResponse
from app.services import dashboard as service
from app.services import instance_settings as org_settings

router = APIRouter(tags=["platform"])
log = get_logger("api.platform")

# Phase 1 has no user table. Credentials come from the environment so that no
# password is ever committed, and the login path is a single place to replace
# with a real user store in phase 4.
_DEFAULT_USER = os.environ.get("DCIM_ADMIN_USER", "admin")


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for a JWT")
async def login(req: LoginRequest, request: Request,
                session: AsyncSession = Depends(get_session),
                settings: Settings = Depends(get_settings)) -> TokenResponse:
    expected = os.environ.get("DCIM_ADMIN_PASSWORD")
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "DCIM_ADMIN_PASSWORD is not configured")
    ip, agent = audit.client_of(request)
    if req.username != _DEFAULT_USER or not hmac.compare_digest(req.password, expected):
        # Failures are the half of the login record that matters. The username
        # attempted is recorded; the password is never touched, not even to
        # note its length, which would leak into the one table an attacker
        # would most like to read.
        await audit.record(session, actor=req.username or "anonymous",
                           action="auth.login", ip=ip, user_agent=agent,
                           outcome="denied")
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")
    await audit.record(session, actor=req.username, action="auth.login",
                       ip=ip, user_agent=agent, after={"role": "admin"})
    await session.commit()
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


async def _stored_org(session: AsyncSession) -> org_settings.StoredOrg:
    """The saved name, or nothing if the database cannot say.

    The login card reads the name before anyone has signed in. A database that
    is down, or not migrated yet, must cost the heading its saved name - it
    falls back to DCIM_ORG_NAME - and never the login page itself.
    """
    try:
        return await org_settings.load(session)
    except Exception as exc:
        log.warning("instance settings unreadable; using the environment", error=str(exc))
        with contextlib.suppress(Exception):
            await session.rollback()
        return org_settings.StoredOrg(None, None, None)


@router.get("/instance", summary="Who this instance belongs to")
async def instance(session: AsyncSession = Depends(get_session),
                   settings: Settings = Depends(get_settings)) -> dict:
    """Identity for the shell: the organisation, and which environment it is.

    Deliberately unauthenticated and deliberately thin. It is read before a
    login form is rendered, so it must not need a token, and it must not carry
    anything an unauthenticated caller should not see - no site names, no
    counts, nothing about the estate itself.
    """
    stored = await _stored_org(session)
    name, _ = org_settings.resolve(stored.org_name, settings.org_name)
    return {"org_name": name, "environment": settings.environment}


class OrganisationUpdate(BaseModel):
    #: Blank or null clears the saved name, handing it back to DCIM_ORG_NAME.
    org_name: str | None = None


def _organisation_view(stored: org_settings.StoredOrg, settings: Settings) -> dict:
    name, source = org_settings.resolve(stored.org_name, settings.org_name)
    return {
        "org_name": name,
        "source": source,
        "saved_name": stored.org_name,
        "environment_name": (settings.org_name or "").strip() or None,
        "default_name": org_settings.PRODUCT_NAME,
        "max_length": org_settings.MAX_ORG_NAME,
        "updated_at": stored.updated_at,
        "updated_by": stored.updated_by,
    }


@router.get("/settings/organisation", summary="The organisation name and where it comes from")
async def get_organisation(session: AsyncSession = Depends(get_session),
                           settings: Settings = Depends(get_settings),
                           _: Principal = Depends(current_principal)) -> dict:
    return _organisation_view(await org_settings.load(session), settings)


@router.put("/settings/organisation", summary="Rename the organisation (admin)")
async def put_organisation(body: OrganisationUpdate, request: Request,
                           session: AsyncSession = Depends(get_session),
                           settings: Settings = Depends(get_settings),
                           p: Principal = Depends(require_role("admin"))) -> dict:
    try:
        name = org_settings.clean_org_name(body.org_name)
    except org_settings.OrgNameError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            {"message": str(exc), "field": "org_name"}) from exc
    before = await org_settings.load(session)
    await org_settings.save(session, name, p.username)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=p.username, action="settings.organisation.update",
                       target_type="instance_setting", target_id="org_name",
                       before={"org_name": before.org_name}, after={"org_name": name},
                       ip=ip, user_agent=agent)
    await session.commit()
    return _organisation_view(await org_settings.load(session), settings)


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
    #
    # The lookback window this used to carry made it lie in the one case that
    # matters: with `WHERE ts > now() - interval '1 hour'`, an outage longer
    # than an hour empties the window, max(ts) comes back NULL, and the check
    # read null and passed. The longer the pipeline had been dead, the
    # healthier it looked. It is unbounded now, so the number grows instead of
    # disappearing, and a table with no rows at all is its own answer.
    #
    # Presence comes from the max and not from a count. `count(*) > 0` here was
    # a full scan of a 57-million-row hypertable - 60 seconds, measured - which
    # is a readiness probe that fails its own deadline and would have an
    # orchestrator restarting a healthy process. A table with no rows has no
    # maximum, which answers the same question for 4.7 ms.
    try:
        row = (await session.execute(text("""
            SELECT extract(epoch FROM (now() - max(ts))) AS age_s
            FROM telemetry_sample
        """))).mappings().first()
        age = row["age_s"] if row else None
        present = age is not None
        checks["telemetry_age_seconds"] = float(age) if age is not None else None
        checks["telemetry_present"] = present
        if not present:
            checks["ingest"] = "no telemetry has ever been written"
            ok = False
        elif age is not None and float(age) > 300:
            checks["ingest"] = "stalled"
            ok = False
    except Exception as exc:
        checks["telemetry_age_seconds"] = f"error: {exc}"

    # Pipeline latency, second-hand from the worker's heartbeat. Distinct from
    # the freshness above: freshness is bounded by the poll interval even in
    # perfect health, while this is publish-to-commit and lives under a second.
    try:
        redis2 = Redis.from_url(settings.redis_url)
        try:
            hb = await platform_monitor.read_heartbeat(redis2)
        finally:
            await redis2.aclose()
        # Reported, not fatal on its own. Fresh telemetry proves a worker is
        # running whatever its build reports, and failing readiness for a
        # heartbeat this instance cannot see would pull a healthy API out of
        # the load balancer during a rolling worker upgrade. When ingestion has
        # genuinely stopped, the telemetry check above has already failed.
        if hb is None:
            checks["ingest_worker"] = "no heartbeat (see telemetry_age_seconds)"
        else:
            checks["ingest_worker_age_seconds"] = round(hb["age_s"], 1)
            checks["ingest_lag_seconds"] = hb.get("lag_s")
            if hb["age_s"] > platform_rules.WORKER_STALE_S:
                checks["ingest_worker"] = "stale"
    except Exception as exc:
        checks["ingest_worker"] = f"error: {exc}"

    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": ok, "checks": checks}
