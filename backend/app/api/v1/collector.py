"""Collector-facing endpoints.

Authenticated with a collector-scoped token, never a user JWT: the assignment
response contains decrypted device credentials.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_collector
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
    _: str = Depends(require_collector),
):
    assignment = await service.build_assignment(session, collector_id, protocol)
    etag = service.etag_for(assignment)

    # Audit every fetch: this is the one endpoint that hands out secrets.
    log.info("assignment fetch", collector_id=collector_id,
             client=request.client.host if request.client else None,
             endpoints=len(assignment.endpoints), etag=etag)

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
