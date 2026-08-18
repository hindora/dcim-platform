"""Datacenters, rooms, rows, racks and the rack elevation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.repositories import racks as repo
from app.schemas import RackElevation, RackSummary
from app.services import devices as service

router = APIRouter(tags=["infrastructure"])


@router.get("/datacenters", summary="List datacenters")
async def list_datacenters(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    return {"items": await repo.list_datacenters(session)}


@router.get("/rooms", summary="List rooms")
async def list_rooms(
    datacenter_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    return {"items": await repo.list_rooms(session, datacenter_id)}


@router.get("/rooms/{room_id}/rows", summary="List rows in a room")
async def list_rows(
    room_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    return {"items": await repo.list_rows(session, room_id)}


@router.get("/racks", response_model=dict, summary="List racks with roll-ups")
async def list_racks(
    room_id: str | None = None,
    datacenter_id: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    items: list[RackSummary] = await service.list_racks(
        session, room_id=room_id, datacenter_id=datacenter_id, limit=limit)
    return {"items": items}


@router.get("/racks/{rack_id}/elevation", response_model=RackElevation,
            summary="Full rack elevation in one request")
async def rack_elevation(
    rack_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> RackElevation:
    elevation = await service.rack_elevation(session, rack_id)
    if elevation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rack not found")
    return elevation
