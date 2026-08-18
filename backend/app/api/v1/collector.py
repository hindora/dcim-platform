"""Collector-facing endpoints.

Authenticated with a collector-scoped token, never a user JWT: the assignment
response contains decrypted device credentials.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_collector
from app.db.session import get_session
from app.repositories import collector as repo
from app.repositories import dashboard as dashboard_repo
from app.schemas import Assignment
from app.services import collector as service

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
