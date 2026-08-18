"""Device endpoints. Routing and validation only - logic lives in the service."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.schemas import (
    DeviceDetail,
    DeviceStateOut,
    DeviceSummary,
    HistoryOut,
    InterfaceOut,
    Page,
)
from app.services import dashboard as dashboard_service
from app.services import devices as service

router = APIRouter(prefix="/devices", tags=["devices"])


@router.get("", response_model=Page[DeviceSummary], summary="List devices")
async def list_devices(
    device_type: list[str] | None = Query(None),
    status_filter: list[str] | None = Query(None, alias="status"),
    room_id: str | None = None,
    rack_id: str | None = None,
    datacenter_id: str | None = None,
    search: str | None = Query(None, max_length=128),
    include_decommissioned: bool = False,
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> Page[DeviceSummary]:
    items, next_cursor = await service.list_devices(
        session, device_types=device_type, status=status_filter, room_id=room_id,
        rack_id=rack_id, datacenter_id=datacenter_id, search=search,
        include_decommissioned=include_decommissioned, limit=limit, cursor=cursor)
    return Page[DeviceSummary](items=items, next_cursor=next_cursor)


@router.get("/{device_id}", response_model=DeviceDetail, summary="Device detail")
async def get_device(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> DeviceDetail:
    device = await service.get_device(session, device_id)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
    return device


@router.get("/{device_id}/state", response_model=DeviceStateOut,
            summary="Current state and hot metrics")
async def get_state(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> DeviceStateOut:
    state = await service.get_state(session, device_id)
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "no state yet for this device")
    return state


@router.get("/{device_id}/interfaces", response_model=list[InterfaceOut])
async def list_interfaces(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> list[InterfaceOut]:
    return await service.list_interfaces(session, device_id)


@router.get("/{device_id}/metrics", summary="Latest value of every metric reported")
async def latest_metrics(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    return {"device_id": device_id,
            "metrics": await dashboard_service.latest(session, device_id)}


@router.get("/{device_id}/history", response_model=HistoryOut,
            summary="Historical telemetry")
async def history(
    device_id: str,
    metric: list[str] = Query(..., description="repeatable"),
    start: datetime | None = None,
    end: datetime | None = None,
    interval: str = Query("auto", pattern="^(auto|raw|1m|5m|1h)$"),
    agg: str = Query("avg", pattern="^(avg|min|max|last)$"),
    instance: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> HistoryOut:
    return await dashboard_service.history(
        session, device_id=device_id, metrics=metric, start=start, end=end,
        interval=interval, agg=agg, instance=instance)
