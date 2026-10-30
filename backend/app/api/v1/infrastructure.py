"""Datacenters, rooms, rows, racks and the rack elevation."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import racks as repo
from app.schemas import (
    FloorPlan,
    RackDetail,
    RackElevation,
    RackSummary,
    ThermalField,
    TwinHistoryRange,
    TwinRoomScene,
    TwinSiteScene,
)
from app.services import devices as service
from app.services import placement
from app.services import racks as rack_service

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


@router.get("/racks/{rack_id}/detail", response_model=RackDetail,
            summary="One rack: space, power, cooling, devices and alarms")
async def rack_detail(
    rack_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> RackDetail:
    out = await rack_service.rack_detail(session, rack_id)
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rack not found")
    return out


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
    # Asset data, edited like the rest of the inventory: operator and up.
    principal: Principal = Depends(require_role("operator")),
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


class Placement(BaseModel):
    """Where a footprint's centre stands, in room metres from the room's origin corner."""

    x: float = Field(ge=0, le=500)
    y: float = Field(ge=0, le=500)
    # Floor-standing units only; None keeps the facing the unit has.
    rotation_deg: float | None = Field(default=None, ge=0, lt=360)


async def _placed(session: AsyncSession, request: Request, principal: Principal, *,
                  kind: str, object_id: str, before: dict, after: dict) -> dict:
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="geometry.place",
                       target_type=kind, target_id=object_id, ip=ip, user_agent=agent,
                       before=before, after=after)
    await session.commit()
    return {"ok": True, **after}


@router.put("/twin/racks/{rack_id}/placement",
            summary="Correct where a rack stands on the floor plan")
async def place_rack(
    rack_id: str,
    body: Placement,
    request: Request,
    session: AsyncSession = Depends(get_session),
    # The drawing is asset data, edited like the rest of the inventory.
    principal: Principal = Depends(require_role("operator")),
) -> dict:
    """The rack is drawn at the new position and no import moves it again.
    Refused with 409 when the footprint leaves the room or lands on another."""
    try:
        before, after = await placement.move_rack(session, rack_id, x=body.x, y=body.y)
    except placement.PlacementNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rack not found") from None
    except placement.PlacementRefusedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            {"message": str(exc), "problems": exc.problems}) from None
    return await _placed(session, request, principal, kind="rack", object_id=rack_id,
                         before=before, after=after)


@router.put("/twin/devices/{device_id}/placement",
            summary="Correct where a floor-standing unit stands on the floor plan")
async def place_unit(
    device_id: str,
    body: Placement,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict:
    try:
        before, after = await placement.move_unit(session, device_id, x=body.x, y=body.y,
                                                  rotation_deg=body.rotation_deg)
    except placement.PlacementNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "no floor-standing device with a footprint by that id") from None
    except placement.PlacementRefusedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            {"message": str(exc), "problems": exc.problems}) from None
    return await _placed(session, request, principal, kind="device", object_id=device_id,
                         before=before, after=after)


@router.delete("/twin/{kind}/{object_id}/placement",
               summary="Hand a hand-placed rack or unit back to the geometry import")
async def release_placement(
    kind: str,
    object_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict:
    """The position holds until the next geometry import, which owns it again."""
    if kind not in ("racks", "devices"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    target = "rack" if kind == "racks" else "device"
    was = await placement.release(session, target, object_id)
    if was is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{target} not found")
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="geometry.release",
                       target_type=target, target_id=object_id, ip=ip, user_agent=agent,
                       before={"geometry_source": was}, after={"geometry_source": "import"})
    await session.commit()
    return {"ok": True, "geometry_source": "import"}


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


@router.get("/twin/rooms/{room_id}/history", response_model=TwinHistoryRange,
            summary="How far back a room can be replayed")
async def room_history(
    room_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> TwinHistoryRange:
    r = await service.room_history_range(session, room_id)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "room not found, or nothing in it is positioned")
    return r


@router.get("/twin/rooms/{room_id}/frame", response_model=TwinRoomScene,
            summary="The room as it was at a moment in the past")
async def room_frame(
    room_id: str,
    t: datetime = Query(..., description="The moment, ISO 8601 with offset"),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> TwinRoomScene:
    """Same shape as the live scene (docs/27 D8): today's geometry, the
    readings at `t` from the hypertable (newest two hours), the five-minute
    rollup (two days) or the hourly one, and the alarms open at `t`."""
    if t.tzinfo is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "t needs a UTC offset")
    scene = await service.room_frame(session, room_id, t)
    if scene is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "room not found, or nothing in it is positioned")
    return scene


@router.get("/twin/rooms/{room_id}/field", response_model=ThermalField,
            summary="The room's air on three planes, interpolated per aisle")
async def room_field(
    room_id: str,
    t: datetime | None = Query(
        None, description="A past moment (ISO 8601 with offset); omit for now"),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> ThermalField:
    """Cold aisles from the intake readings on the faces that open onto
    them, hot aisles from the exhausts; never blended across a rack row.
    Each cell carries a confidence so the viewer fades unmeasured air."""
    if t is not None and t.tzinfo is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "t needs a UTC offset")
    f = await service.room_field(session, room_id, t)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "room not found, or nothing in it is positioned")
    return f
