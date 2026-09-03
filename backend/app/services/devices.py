"""Device and rack business logic. No SQL, no FastAPI imports."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import devices as repo
from app.repositories import racks as rack_repo
from app.repositories import tags as tag_repo
from app.schemas import (
    DeviceDetail,
    DeviceStateOut,
    DeviceSummary,
    ElevationDevice,
    ElevationSlot,
    EndpointSummary,
    FloorAisle,
    FloorEquipment,
    FloorPlan,
    FloorRack,
    FreeBlock,
    InterfaceOut,
    LocationRef,
    MetricValue,
    PowerSupplyOut,
    RackElevation,
    RackSummary,
    RoomExtent,
)
from app.services import endpoint_config, floorplan
from app.services import poll_profile_config as cfg


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
        asset_tag=row.get("asset_tag"), serial_number=row.get("serial_number"),
        lifecycle=row.get("lifecycle") or "in_service",
        category=row.get("category"),
        warranty_expires=row.get("warranty_expires"),
        warranty_state=row.get("warranty_state") or "unknown",
        owner_group=row.get("owner_group"),
        cost_centre=row.get("cost_centre"),
    )


async def list_devices(session: AsyncSession, *, with_total: bool = False,
                       **kwargs) -> tuple[list[DeviceSummary], str | None, int | None]:
    rows, cursor = await repo.list_devices(session, **kwargs)
    items = [_summary(r) for r in rows]
    # One query for every row's tags rather than one per row: the asset table
    # renders 200 of them, and a per-row fetch is 200 round trips behind one
    # screen.
    if items:
        by_device = await tag_repo.tags_for_devices(session, [d.id for d in items])
        for device in items:
            device.tags = by_device.get(device.id, [])

    total = None
    if with_total:
        # Opt-in, because it is a second query and the callers that only want
        # the next page should not pay for it. The paging UI asks; the rest
        # do not.
        paging = {"limit", "cursor"}
        total = await repo.count_matching(
            session, **{k: v for k, v in kwargs.items() if k not in paging})
    return items, cursor, total


async def get_device(session: AsyncSession, device_id: str) -> DeviceDetail | None:
    row = await repo.get_device(session, device_id)
    if row is None:
        return None
    endpoints = [EndpointSummary(**e) for e in await repo.list_endpoints(session, device_id)]
    # serial_number, asset_tag, lifecycle and category now come from the
    # summary itself - they were promoted onto DeviceSummary for the asset
    # list. Passing them again here is a duplicate-keyword TypeError, so the
    # detail only adds what the summary does not carry.
    base = _summary(row).model_dump()
    return DeviceDetail(
        **base,
        u_height=row.get("u_height") or 1, facing=row.get("facing"),
        admin_state=row.get("admin_state") or "enabled",
        attributes=row.get("attributes") or {},
        rated_power_w=row.get("rated_power_w"),
        psus=[PowerSupplyOut(**ps)
              for ps in await repo.list_power_supplies(session, device_id)],
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
    zero_u: list[ElevationDevice] = []

    for d in devices:
        entry = ElevationDevice(
            id=d["id"], name=d["name"], device_type=d["device_type"],
            status=d["status"], health=d["health"], max_severity=d["max_severity"],
            power_w=_f(d.get("power_w")), inlet_temp_c=_f(d.get("inlet_temp_c")),
            cpu_util_pct=_f(d.get("cpu_util_pct")),
        )
        if d.get("u_start") is None:
            zero_u.append(entry)
            continue
        occupied.append((d["u_start"], d.get("u_height") or 1))
        positions.append(ElevationSlot(
            u_start=d["u_start"], u_height=d.get("u_height") or 1,
            facing=d.get("facing"), free=False, device=entry))

    blocks = rack_repo.compute_free_blocks(u_height, occupied)
    for b in blocks:
        positions.append(ElevationSlot(u_start=b["u_start"], u_height=b["u_height"],
                                       free=True, device=None))
    positions.sort(key=lambda p: -p.u_start)

    return RackElevation(
        rack=_rack_summary(rack), positions=positions,
        free_blocks=[FreeBlock(**b) for b in blocks],
        zero_u_devices=zero_u,
    )


async def room_floorplan(session: AsyncSession, room_id: str) -> FloorPlan | None:
    """Everything needed to draw one room, in a single request."""
    racks = await rack_repo.floorplan_racks(session, room_id)
    equipment = await rack_repo.floorplan_equipment(session, room_id)
    if not racks and not equipment:
        return None

    # Racks only. They are the sole things with a real room coordinate.
    points = [(float(r["floor_x"]), float(r["floor_y"])) for r in racks]

    first = racks[0] if racks else None
    return FloorPlan(
        room_id=room_id,
        room_name=(first or {}).get("room_name") or "",
        datacenter_code=(first or {}).get("datacenter_code"),
        extent=RoomExtent(**floorplan.room_extent(points)),
        rack_w_m=floorplan.RACK_W, rack_d_m=floorplan.RACK_D,
        racks=[FloorRack(
            id=r["id"], name=r["name"], row_name=r.get("row_name"),
            x=float(r["floor_x"]), y=float(r["floor_y"]), facing=r.get("facing"),
            device_count=r.get("device_count") or 0,
            offline_count=r.get("offline_count") or 0,
            load_kw=_f(r.get("load_kw")), max_inlet_c=_f(r.get("max_inlet_c")),
            max_severity=r.get("max_severity") or "CLEAR",
            free_u=r.get("free_u"),
        ) for r in racks],
        unpositioned_equipment=[FloorEquipment(
            id=e["id"], name=e["name"], device_type=e["device_type"],
            status=e.get("status") or "UNKNOWN",
            max_severity=e.get("max_severity") or "CLEAR",
            power_w=_f(e.get("power_w")),
        ) for e in equipment],
        aisles=[FloorAisle(y_start=a.y_start, y_end=a.y_end, kind=a.kind,
                           label=a.label, rows=a.rows)
                for a in floorplan.derive_aisles(racks)],
    )


class PollProfileNotFoundError(LookupError):
    """No profile with that id."""


class EndpointNotFoundError(LookupError):
    """No endpoint with that id on that device."""


async def endpoints(session: AsyncSession, device_id: str
                    ) -> list[EndpointSummary]:
    return [EndpointSummary(**e)
            for e in await repo.list_endpoints(session, device_id)]


async def one_endpoint(session: AsyncSession, device_id: str,
                       endpoint_id: str) -> EndpointSummary:
    for e in await endpoints(session, device_id):
        if e.id == endpoint_id:
            return e
    raise EndpointNotFoundError(endpoint_id)


#: Fields whose old value is worth recording in the audit trail.
#:
#: Not the whole row: an audit entry that repeats everything hides the one
#: thing that moved, and this is the trail somebody reads after a device went
#: quiet at 3am.
_AUDITED = ("address", "port", "addressing", "credential_id",
            "poll_profile_id", "enabled", "admin_state")


async def update_endpoint(session: AsyncSession, *, device_id: str,
                          endpoint_id: str, changes: dict[str, Any]
                          ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate an endpoint edit, apply it, and return what moved.

    Validation happens against the STORED row rather than the request, because
    the rules that matter are relational: whether this endpoint sits behind a
    gateway, whether the chosen credential speaks this protocol. A request
    carries none of that.
    """
    current = await repo.get_endpoint(session, endpoint_id)
    if current is None or current["device_id"] != device_id:
        raise EndpointNotFoundError(endpoint_id)

    touched = set(changes)
    endpoint_config.check_addressable(current, touched)
    endpoint_config.check_trap_endpoint(current, touched)

    clean: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "port":
            clean[key] = endpoint_config.validate_port(value)
        elif key == "addressing":
            clean[key] = endpoint_config.validate_addressing(
                current["protocol"], value or {})
        elif key == "credential_id" and value is not None:
            cred = await repo.get_credential(session, value)
            if cred is None:
                raise endpoint_config.EndpointConfigError(
                    "no such credential")
            endpoint_config.check_credential(current["protocol"], cred)
            clean[key] = value
        elif key == "poll_profile_id" and value is not None:
            if await repo.get_poll_profile(session, value) is None:
                raise endpoint_config.EndpointConfigError(
                    "no such poll profile")
            clean[key] = value
        else:
            clean[key] = value

    # Only what actually differs. An edit that changes nothing should not bump
    # updated_at: that version is what every collector polls against, and a
    # no-op save would hand the whole fleet a new assignment for nothing.
    effective = {k: v for k, v in clean.items() if current.get(k) != v}
    if not effective:
        return {}, {}

    before = {k: current.get(k) for k in effective if k in _AUDITED}
    await repo.update_endpoint(session, endpoint_id, effective)
    return before, {k: v for k, v in effective.items() if k in _AUDITED}


async def credentials(session: AsyncSession, *, protocol: str | None = None,
                      q: str | None = None, current: str | None = None
                      ) -> list[dict[str, Any]]:
    return await repo.list_credentials(session, protocol=protocol, q=q,
                                       include_id=current)


async def credential_count(session: AsyncSession, protocol: str | None = None
                           ) -> int:
    return await repo.count_credentials(session, protocol)


async def poll_profiles(session: AsyncSession) -> list[dict[str, Any]]:
    return await repo.list_poll_profiles(session)


async def poll_profile_usage(session: AsyncSession, profile_id: str
                             ) -> list[dict[str, Any]]:
    return await repo.poll_profile_usage(session, profile_id)


async def create_poll_profile(session: AsyncSession, body: dict[str, Any]
                              ) -> dict[str, Any]:
    """Validate and insert. Defaults are stated rather than left to the table.

    A profile created with the column defaults would be a 30-second poll with
    no groups - collecting nothing, on every endpoint moved onto it - so the
    form's values are required to be complete here even though the schema
    would accept less.
    """
    existing = {p["name"] for p in await repo.list_poll_profiles(session)}
    clean = cfg.validate(body, existing_names=existing, creating=True)
    values = {
        "name": clean["name"],
        "interval_s": clean.get("interval_s", 60),
        "timeout_ms": clean.get("timeout_ms", 5000),
        "retries": clean.get("retries", 1),
        "metric_groups": clean.get("metric_groups", []),
        "push_enabled": clean.get("push_enabled", False),
    }
    cfg.check_timing(values["interval_s"], values["timeout_ms"],
                     values["retries"])
    return await repo.create_poll_profile(session, values)


async def update_poll_profile(session: AsyncSession, profile_id: str,
                              changes: dict[str, Any]
                              ) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Validate an edit against the stored profile and apply it.

    Returns the before and after of what moved, plus how many endpoints follow
    this profile - the caller records that in the audit trail, because the
    blast radius is the part worth being able to look up later.
    """
    current = await repo.get_poll_profile(session, profile_id)
    if current is None:
        raise PollProfileNotFoundError(profile_id)

    # The name is deliberately not editable. app/importer/endpoints.py selects
    # profiles by name - poll_profile="snmp-server-120s" - so renaming one
    # silently breaks the next import, which lands endpoints on a default
    # profile instead of failing.
    if "name" in changes and changes["name"] != current["name"]:
        raise cfg.PollProfileError(
            "a profile cannot be renamed: the importer selects profiles by "
            "name, and a rename would send the next import to a different "
            "profile without failing. Create a new one and move the endpoints.")

    clean = cfg.validate(changes, creating=False)
    merged = {**current, **clean}
    cfg.check_timing(merged["interval_s"], merged["timeout_ms"],
                     merged["retries"])

    effective = {k: v for k, v in clean.items()
                 if k in ("interval_s", "timeout_ms", "retries",
                          "metric_groups", "push_enabled")
                 and current.get(k) != v}
    if not effective:
        return {}, {}, 0

    before = {k: current.get(k) for k in effective}
    await repo.update_poll_profile(session, profile_id, effective)
    followers = sum(u["endpoints"]
                    for u in await repo.poll_profile_usage(session, profile_id))
    return before, effective, followers
