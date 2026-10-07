"""Datacenters, rooms, rows, racks and the rack elevation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.repositories import racks as repo
from app.schemas import (
    FloorPlan,
    RackElevation,
    RackSummary,
    TwinRoomScene,
    TwinSiteScene,
)
from app.services import devices as service

router = APIRouter(tags=["infrastructure"])


def _num(v: object) -> float | None:
    return float(v) if v is not None else None  # type: ignore[arg-type]


class SiteLocation(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


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


@router.get("/rooms/{room_id}/floorplan", response_model=FloorPlan,
            summary="Room floor plan with rack positions and aisles")
async def room_floorplan(
    room_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> FloorPlan:
    plan = await service.room_floorplan(session, room_id)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "room not found, or nothing in it is positioned")
    return plan


@router.get("/twin/sites/{datacenter_id}/scene", response_model=TwinSiteScene,
            summary="A site as a building: levels and placed rooms")
async def site_scene(
    datacenter_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> TwinSiteScene:
    scene = await service.site_scene(session, datacenter_id)
    if scene is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "site not found")
    return scene


@router.put("/datacenters/{datacenter_id}/location",
            summary="Set a site's position on the world map")
async def set_site_location(
    datacenter_id: str,
    body: SiteLocation,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> dict:
    """The surveyed position replaces a city-centroid estimate, and no import
    overwrites it afterwards."""
    before = (await session.execute(text("""
        SELECT latitude, longitude, location_source FROM datacenter
         WHERE id = CAST(:dc AS uuid)
    """), {"dc": datacenter_id})).mappings().first()
    if before is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "site not found")
    await session.execute(text("""
        UPDATE datacenter SET latitude = :lat, longitude = :lon, location_source = 'manual'
         WHERE id = CAST(:dc AS uuid)
    """), {"dc": datacenter_id, "lat": body.latitude, "lon": body.longitude})
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="site.location", target_type="datacenter",
                       target_id=datacenter_id, ip=ip, user_agent=agent,
                       before={"latitude": _num(before["latitude"]),
                               "longitude": _num(before["longitude"]),
                               "location_source": before["location_source"]},
                       after={"latitude": body.latitude, "longitude": body.longitude,
                              "location_source": "manual"})
    await session.commit()
    return {"ok": True, "latitude": body.latitude, "longitude": body.longitude,
            "location_source": "manual"}


@router.get("/twin/rooms/{room_id}/scene", response_model=TwinRoomScene,
            summary="One room in 3D: geometry, state and rack contents")
async def room_scene(
    room_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> TwinRoomScene:
    scene = await service.room_scene(session, room_id)
    if scene is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "room not found, or nothing in it is positioned")
    return scene
