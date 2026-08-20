"""Discovery endpoints. Routing and validation only."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.repositories import discovery as repo
from app.services import discovery as service

router = APIRouter(prefix="/discovery", tags=["discovery"])


class RunRequest(BaseModel):
    method: str = "snmp_sweep"
    subnets: list[str] = Field(default_factory=list,
                               examples=[["10.51.0.0/24"]])


class PromoteRequest(BaseModel):
    name: str
    device_type: str | None = None


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED,
             summary="Queue a discovery sweep")
async def create_run(req: RunRequest,
                     session: AsyncSession = Depends(get_session),
                     _: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Queues the run; a collector claims and executes it.

    Accepted rather than created: the sweep happens on the management network,
    which is where the collector is and the API is not.
    """
    try:
        run = await service.create_run(session, method=req.method,
                                       subnets=req.subnets)
        await session.commit()
        return run
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/runs", summary="List discovery runs")
async def list_runs(limit: int = Query(25, ge=1, le=100),
                    session: AsyncSession = Depends(get_session),
                    _: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {"items": await repo.list_runs(session, limit)}


@router.get("/candidates", summary="What answered, and whether we knew about it")
async def list_candidates(
    run_id: str | None = None,
    candidate_status: str | None = Query(None, alias="status"),
    unmatched_only: bool = Query(
        False, description="Only responders inventory has never heard of"),
    limit: int = Query(200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    items = await repo.list_candidates(
        session, run_id=run_id, status=candidate_status,
        unmatched_only=unmatched_only, limit=limit)
    return {"items": items,
            "unmanaged": sum(1 for i in items if not i["matched_device_id"])}


@router.post("/candidates/{candidate_id}/promote",
             summary="Create an inventory device from a candidate")
async def promote(candidate_id: str, req: PromoteRequest,
                  session: AsyncSession = Depends(get_session),
                  _: Principal = Depends(current_principal)) -> dict[str, Any]:
    try:
        result = await service.promote(session, candidate_id, req.model_dump())
        await session.commit()
        return result
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.post("/candidates/{candidate_id}/ignore",
             summary="Dismiss a candidate")
async def ignore(candidate_id: str,
                 session: AsyncSession = Depends(get_session),
                 _: Principal = Depends(current_principal)) -> dict[str, Any]:
    try:
        result = await service.ignore(session, candidate_id)
        await session.commit()
        return result
    except service.DiscoveryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
