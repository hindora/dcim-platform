"""Device and rack business logic. No SQL, no FastAPI imports."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import devices as repo
from app.repositories import racks as rack_repo
from app.schemas import (
    DeviceDetail,
    DeviceStateOut,
    DeviceSummary,
    ElevationDevice,
    ElevationSlot,
    EndpointSummary,
    FreeBlock,
    InterfaceOut,
    LocationRef,
    MetricValue,
    RackElevation,
    RackSummary,
)


def _location(row: dict[str, Any]) -> LocationRef:
    return LocationRef(
        datacenter_id=row.get("datacenter_id"),
        datacenter_code=row.get("datacenter_code"),
        room_id=row.get("room_id"),
        room_name=row.get("room_name"),
        row_name=row.get("row_name"),
        rack_id=row.get("rack_id"),
        rack_name=row.get("rack_name"),
        u_start=row.get("u_start"),
    )


def _summary(row: dict[str, Any]) -> DeviceSummary:
    return DeviceSummary(
        id=row["id"], name=row["name"], device_type=row["device_type"],
        vendor=row.get("vendor"), model=row.get("model"),
        status=row.get("status") or "UNKNOWN",
        health=row.get("health") or "UNKNOWN",
        max_severity=row.get("max_severity") or "CLEAR",
        mgmt_ip=row.get("mgmt_ip"), primary_ip=row.get("primary_ip"),
        last_seen=row.get("last_seen"), location=_location(row),
    )


async def list_devices(session: AsyncSession, **kwargs) -> tuple[list[DeviceSummary], str | None]:
    rows, cursor = await repo.list_devices(session, **kwargs)
    return [_summary(r) for r in rows], cursor


async def get_device(session: AsyncSession, device_id: str) -> DeviceDetail | None:
    row = await repo.get_device(session, device_id)
    if row is None:
        return None
    endpoints = [EndpointSummary(**e) for e in await repo.list_endpoints(session, device_id)]
    base = _summary(row).model_dump()
    return DeviceDetail(
        **base,
        serial_number=row.get("serial_number"), asset_tag=row.get("asset_tag"),
        u_height=row.get("u_height") or 1, facing=row.get("facing"),
        lifecycle=row.get("lifecycle") or "in_service",
        admin_state=row.get("admin_state") or "enabled",
        attributes=row.get("attributes") or {},
        endpoints=endpoints,
    )


async def get_state(session: AsyncSession, device_id: str) -> DeviceStateOut | None:
    row = await repo.get_device_state(session, device_id)
    if row is None:
        return None
    metrics = {
        k: MetricValue(v=v.get("v"), t=v.get("t"), q=v.get("q", "good"))
        for k, v in (row.get("metrics") or {}).items()
    }
    return DeviceStateOut(
        device_id=row["device_id"], status=row["status"], health=row["health"],
        max_severity=row["max_severity"], active_alarms=row.get("active_alarms") or 0,
        last_seen=row.get("last_seen"), metrics=metrics,
    )


async def list_interfaces(session: AsyncSession, device_id: str) -> list[InterfaceOut]:
    return [InterfaceOut(**r) for r in await repo.list_interfaces(session, device_id)]


# ------------------------------------------------------------------- racks

def _rack_summary(row: dict[str, Any]) -> RackSummary:
    return RackSummary(
        id=row["id"], name=row["name"], row_name=row.get("row_name"),
        room_id=row.get("room_id"), room_name=row.get("room_name"),
        datacenter_code=row.get("datacenter_code"),
        u_height=row.get("u_height") or 42,
        device_count=row.get("device_count") or 0,
        online_count=row.get("online_count") or 0,
        offline_count=row.get("offline_count") or 0,
        load_kw=_f(row.get("load_kw")), rated_power_kw=_f(row.get("rated_power_kw")),
        load_pct=_f(row.get("load_pct")), max_inlet_c=_f(row.get("max_inlet_c")),
        max_severity=row.get("max_severity") or "CLEAR",
        free_u=row.get("free_u"),
    )


def _f(v: Any) -> float | None:
    return float(v) if v is not None else None


async def list_racks(session: AsyncSession, **kwargs) -> list[RackSummary]:
    return [_rack_summary(r) for r in await rack_repo.list_racks(session, **kwargs)]


async def rack_elevation(session: AsyncSession, rack_id: str) -> RackElevation | None:
    rack = await rack_repo.get_rack(session, rack_id)
    if rack is None:
        return None
    devices = await rack_repo.rack_devices(session, rack_id)
    u_height = rack.get("u_height") or 42

    positions: list[ElevationSlot] = []
    occupied: list[tuple[int, int]] = []
    for d in devices:
        occupied.append((d.get("u_start"), d.get("u_height") or 1))
        positions.append(ElevationSlot(
            u_start=d.get("u_start") or 0, u_height=d.get("u_height") or 1,
            facing=d.get("facing"), free=False,
            device=ElevationDevice(
                id=d["id"], name=d["name"], device_type=d["device_type"],
                status=d["status"], health=d["health"], max_severity=d["max_severity"],
                power_w=_f(d.get("power_w")), inlet_temp_c=_f(d.get("inlet_temp_c")),
                cpu_util_pct=_f(d.get("cpu_util_pct")),
            )))

    blocks = rack_repo.compute_free_blocks(u_height, occupied)
    for b in blocks:
        positions.append(ElevationSlot(u_start=b["u_start"], u_height=b["u_height"],
                                       free=True, device=None))
    positions.sort(key=lambda p: -p.u_start)

    return RackElevation(
        rack=_rack_summary(rack), positions=positions,
        free_blocks=[FreeBlock(**b) for b in blocks],
    )
