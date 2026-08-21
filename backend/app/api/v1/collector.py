"""Collector-facing endpoints.

Authenticated with a collector-scoped token, never a user JWT: the assignment
response contains decrypted device credentials.
"""

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import (
    UNSCOPED_COLLECTOR,
    Principal,
    current_principal,
    require_collector,
)
from app.db.session import get_session
from app.repositories import collector as repo
from app.repositories import dashboard as dashboard_repo
from app.repositories import discovery as disc_repo
from app.schemas import Assignment
from app.services import collector as service
from app.services import discovery as disc_service

router = APIRouter(prefix="/collector", tags=["collector"])
log = get_logger("api.collector")


@router.get("/assignments", response_model=Assignment,
            summary="Endpoints this collector should poll")
async def assignments(
    request: Request,
    response: Response,
    collector_id: str = Query(..., min_length=1, max_length=64),
    protocol: list[str] | None = Query(None),
    if_none_match: str | None = Header(None, alias="If-None-Match"),
    session: AsyncSession = Depends(get_session),
    identity: str = Depends(require_collector),
):
    """The one endpoint that returns decrypted device credentials.

    It cannot be otherwise - the collector has to authenticate to devices - so
    the mitigations are what matter: a credential type no browser holds, a
    token bound to one collector, and an audit row for every handout.
    """
    ip, agent = audit.client_of(request)

    # Scope enforcement. A token derived for col-1 may not fetch col-2's
    # shard: collector_id arrives as a query parameter, so without this check
    # any holder of any collector token could ask for every other collector's
    # endpoints and be handed the credentials for all of them.
    if identity != UNSCOPED_COLLECTOR and identity != collector_id:
        await audit.record(
            session, actor=f"collector:{identity}", action="credential.denied",
            target_type="collector", target_id=collector_id, ip=ip,
            user_agent=agent, outcome="denied",
            after={"reason": "token is scoped to a different collector"})
        await session.commit()
        log.warning("assignment scope violation", token_identity=identity,
                    requested=collector_id, client=ip)
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "this token is not scoped to that collector")

    assignment = await service.build_assignment(session, collector_id, protocol)
    etag = service.etag_for(assignment)

    with_secrets = sum(1 for e in assignment.endpoints
                       if getattr(e, "credential", None) is not None)
    # Audit every fetch: this is the one endpoint that hands out secrets. The
    # row records how many credentials went out, never which - the count is
    # what an investigation needs, and the list would put the target set of a
    # compromise into a table that is easier to read than the one it protects.
    await audit.record(
        session, actor=f"collector:{identity}", action="credential.fetch",
        target_type="collector", target_id=collector_id, ip=ip,
        user_agent=agent,
        after={"endpoints": len(assignment.endpoints),
               "credentials_returned": with_secrets, "etag": etag,
               "scoped": identity != UNSCOPED_COLLECTOR})
    await session.commit()

    log.info("assignment fetch", collector_id=collector_id, identity=identity,
             client=ip, endpoints=len(assignment.endpoints),
             credentials=with_secrets, etag=etag,
             scoped=identity != UNSCOPED_COLLECTOR)
    if identity == UNSCOPED_COLLECTOR:
        # Visible rather than silent: a fleet-wide token is a standing risk,
        # and it should show up in the log every time it is used, not only in
        # a design document.
        log.warning("unscoped collector token used", collector_id=collector_id,
                    client=ip)

    if if_none_match and if_none_match.strip() == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED,
                        headers={"ETag": etag, "Cache-Control": "no-cache"})

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return assignment


@router.get("/instances", summary="Collector fleet health")
async def instances(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    return {"items": await dashboard_repo.collectors(session)}


@router.post("/heartbeat", status_code=status.HTTP_204_NO_CONTENT,
             summary="Heartbeat (fallback for collectors not using the stream)")
async def heartbeat(
    payload: dict,
    session: AsyncSession = Depends(get_session),
    _: str = Depends(require_collector),
) -> Response:
    import json

    await repo.upsert_heartbeat(session, {
        "id": payload.get("collector_id", "unknown"),
        "version": payload.get("version"),
        "hostname": payload.get("hostname"),
        "started_at": payload.get("started_at"),
        "endpoints_owned": int(payload.get("endpoints_owned") or 0),
        "endpoints_online": int(payload.get("endpoints_online") or 0),
        "stats": json.dumps(payload.get("stats") or {}),
    })
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/discovery/claim",
            summary="Claim a pending discovery run (collector only)")
async def claim_discovery(session: AsyncSession = Depends(get_session),
                          _: str = Depends(require_collector),
                          ) -> dict[str, Any]:
    """Hand one queued sweep to the caller, or nothing.

    The sweep runs on the collector because that is what sits on the management
    network. The API only decides what should be swept and what the answer
    means.
    """
    run = await disc_repo.claim_pending(session)
    # Committed immediately: the claim is the point. Without it the row's
    # status never leaves 'pending' and every collector claims it forever.
    await session.commit()
    return {"run": run}


class DiscoveryResult(BaseModel):
    address: str
    protocol: str = "snmp"
    identity: dict[str, Any] = Field(default_factory=dict)


class DiscoveryResults(BaseModel):
    responders: list[DiscoveryResult] = Field(default_factory=list)
    error: str | None = None


@router.post("/discovery/{run_id}/results",
             summary="Report what a sweep found (collector only)")
async def discovery_results(run_id: str, body: DiscoveryResults,
                            session: AsyncSession = Depends(get_session),
                            _: str = Depends(require_collector),
                            ) -> dict[str, Any]:
    if body.error:
        await disc_repo.finish_run(session, run_id, found=0, status="failed",
                                   error=body.error)
        await session.commit()
        return {"status": "failed"}
    result = await disc_service.record_results(
        session, run_id, [r.model_dump() for r in body.responders])
    await session.commit()
    return result


@router.get("/health", summary="Collector fleet and platform self-monitoring")
async def collector_health(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """What the platform currently believes about itself.

    Read-only: it gathers the same signals the worker's evaluator uses and
    reports the findings, without raising or clearing anything. The worker owns
    the alarm lifecycle - two writers would fight over the same rows, and the
    API has no business deciding the platform is unhealthy on a page load.

    The distinction this page exists to make: silence from the datacenter and
    silence from the monitoring look identical on every other screen.
    """
    from app.alarms import platform as rules
    from app.alarms import platform_monitor
    from app.contracts.messages_gen import Stream
    from app.repositories import alarms as alarm_repo

    redis = Redis.from_url(settings.redis_url)
    try:
        signals = await platform_monitor.gather(
            session, redis,
            streams=[Stream.TELEMETRY, Stream.EVENTS],
            group=settings.ingest_group)
    finally:
        await redis.aclose()

    findings = rules.evaluate(signals)
    open_alarms = await alarm_repo.open_platform_alarms(session)

    return {
        "verdict": rules.summarise(findings),
        "findings": [
            {"alarm_type": f.alarm_type, "instance": f.instance,
             "severity": f.severity, "message": f.message,
             "value": f.value, "threshold": f.threshold}
            for f in findings
        ],
        "open_alarms": open_alarms,
        "pipeline": {
            # Two numbers, never one. Freshness is bounded by the poll interval
            # even when everything is perfect; lag is publish-to-commit and is
            # sub-second when it is not broken.
            "ingest_lag_seconds": signals.ingest_lag_s,
            "telemetry_age_seconds": signals.telemetry_age_s,
            "telemetry_present": signals.telemetry_present,
            "worker_heartbeat_age_seconds": signals.worker_heartbeat_age_s,
            "stream_pending": signals.stream_pending,
            "lag_warning_seconds": rules.INGEST_LAG_WARNING_S,
            "lag_critical_seconds": rules.INGEST_LAG_CRITICAL_S,
        },
        "collectors": [
            {"collector_id": c.collector_id,
             "heartbeat_age_seconds": c.heartbeat_age_s,
             "status": c.status,
             "endpoints_owned": c.endpoints_owned,
             "endpoints_online": c.endpoints_online,
             "stale_after_seconds": rules.COLLECTOR_STALE_S}
            for c in signals.collectors
        ],
    }
