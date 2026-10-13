"""Correcting where a rack or a floor-standing unit stands (docs/27 Phase 6).

The drawing is asset data somebody maintains. A placement written here is
marked `geometry_source = 'manual'`, which every geometry import already leaves
alone; releasing it hands the object back to the import, whose next run puts
it where the source says.

What a move does not do: it does not re-derive the room's stored aisles (an
import-drawn aisle stays where the import drew it), and it does not touch the
devices in the rack, which are placed by U and follow the rack.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import placement as rules
from app.services import floorplan

#: Device mounts that stand on the floor and so have a footprint to place.
#: Wall, ceiling and in-rack mounts are positioned by other means.
FLOOR_MOUNTS = ("floor",)


class PlacementNotFoundError(LookupError):
    """No such rack, or no such floor-standing device."""


class PlacementRefusedError(ValueError):
    """The footprint cannot stand there; `.problems` says why."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _f(v: Any) -> float | None:
    return float(v) if v is not None else None


async def _room_boxes(session: AsyncSession, room_id: str, *, skip_rack: str | None = None,
                      skip_device: str | None = None) -> list[rules.Box]:
    """Every footprint standing in the room, except the one being moved."""
    racks = (await session.execute(text("""
        SELECT r.id::text, r.name, r.floor_x, r.floor_y, r.width_m, r.depth_m
          FROM rack r JOIN rack_row rr ON rr.id = r.row_id
         WHERE rr.room_id = CAST(:room AS uuid) AND r.floor_x IS NOT NULL AND r.floor_y IS NOT NULL
    """), {"room": room_id})).mappings().all()
    units = (await session.execute(text("""
        SELECT d.id::text, d.name, d.floor_x, d.floor_y, d.footprint_w_m, d.footprint_d_m,
               d.rotation_deg
          FROM device d
         WHERE d.room_id = CAST(:room AS uuid) AND d.rack_id IS NULL
           AND d.lifecycle <> 'decommissioned' AND d.mount = ANY(:mounts)
           AND d.floor_x IS NOT NULL AND d.floor_y IS NOT NULL
           AND d.footprint_w_m IS NOT NULL AND d.footprint_d_m IS NOT NULL
    """), {"room": room_id, "mounts": list(FLOOR_MOUNTS)})).mappings().all()
    boxes = [rules.footprint(float(r["floor_x"]), float(r["floor_y"]),
                             _f(r["width_m"]) or floorplan.RACK_W,
                             _f(r["depth_m"]) or floorplan.RACK_D, 0.0, r["name"])
             for r in racks if r["id"] != skip_rack]
    boxes += [rules.footprint(float(u["floor_x"]), float(u["floor_y"]),
                              float(u["footprint_w_m"]), float(u["footprint_d_m"]),
                              _f(u["rotation_deg"]), u["name"])
              for u in units if u["id"] != skip_device]
    return boxes


async def move_rack(session: AsyncSession, rack_id: str, *, x: float,
                    y: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Stand the rack at (x, y), room metres, centre of its footprint.

    Returns (before, after) for the audit trail. Raises `PlacementRefusedError`
    when the footprint leaves the room or lands on another one.
    """
    row = (await session.execute(text("""
        SELECT r.id::text, r.name, r.floor_x, r.floor_y, r.width_m, r.depth_m,
               r.geometry_source, rm.id::text AS room_id, rm.width_m AS room_w,
               rm.depth_m AS room_d
          FROM rack r
          JOIN rack_row rr ON rr.id = r.row_id
          JOIN room rm     ON rm.id = rr.room_id
         WHERE r.id = CAST(:id AS uuid)
    """), {"id": rack_id})).mappings().first()
    if row is None:
        raise PlacementNotFoundError(rack_id)
    box = rules.footprint(x, y, _f(row["width_m"]) or floorplan.RACK_W,
                          _f(row["depth_m"]) or floorplan.RACK_D, 0.0, row["name"])
    found = rules.problems(box, _f(row["room_w"]), _f(row["room_d"]),
                           await _room_boxes(session, row["room_id"], skip_rack=rack_id))
    if found:
        raise PlacementRefusedError(found)
    await session.execute(text("""
        UPDATE rack SET floor_x = :x, floor_y = :y, geometry_source = 'manual'
         WHERE id = CAST(:id AS uuid)
    """), {"id": rack_id, "x": round(x, 2), "y": round(y, 2)})
    before = {"x": _f(row["floor_x"]), "y": _f(row["floor_y"]),
              "geometry_source": row["geometry_source"]}
    after = {"x": round(x, 2), "y": round(y, 2), "geometry_source": "manual"}
    return before, after


async def move_unit(session: AsyncSession, device_id: str, *, x: float, y: float,
                    rotation_deg: float | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Stand a floor-standing unit at (x, y), optionally turned.

    `rotation_deg` None keeps the facing it has. Only floor-mounted devices
    with a footprint qualify: a wall probe or a device in a rack has no
    footprint on the floor to place.
    """
    row = (await session.execute(text("""
        SELECT d.id::text, d.name, d.floor_x, d.floor_y, d.rotation_deg, d.mount,
               d.rack_id, d.footprint_w_m, d.footprint_d_m, d.geometry_source,
               rm.id::text AS room_id, rm.width_m AS room_w, rm.depth_m AS room_d
          FROM device d
          JOIN room rm ON rm.id = d.room_id
         WHERE d.id = CAST(:id AS uuid) AND d.lifecycle <> 'decommissioned'
    """), {"id": device_id})).mappings().first()
    if (row is None or row["rack_id"] is not None or row["mount"] not in FLOOR_MOUNTS
            or row["footprint_w_m"] is None or row["footprint_d_m"] is None):
        raise PlacementNotFoundError(device_id)
    rot = _f(row["rotation_deg"]) if rotation_deg is None else rotation_deg % 360.0
    box = rules.footprint(x, y, float(row["footprint_w_m"]), float(row["footprint_d_m"]),
                          rot, row["name"])
    found = rules.problems(box, _f(row["room_w"]), _f(row["room_d"]),
                           await _room_boxes(session, row["room_id"], skip_device=device_id))
    if found:
        raise PlacementRefusedError(found)
    await session.execute(text("""
        UPDATE device SET floor_x = :x, floor_y = :y, rotation_deg = :rot,
                          geometry_source = 'manual'
         WHERE id = CAST(:id AS uuid)
    """), {"id": device_id, "x": round(x, 2), "y": round(y, 2), "rot": rot})
    before = {"x": _f(row["floor_x"]), "y": _f(row["floor_y"]),
              "rotation_deg": _f(row["rotation_deg"]),
              "geometry_source": row["geometry_source"]}
    after = {"x": round(x, 2), "y": round(y, 2), "rotation_deg": rot,
             "geometry_source": "manual"}
    return before, after


async def release(session: AsyncSession, kind: str, object_id: str) -> str | None:
    """Hand a hand-placed rack or unit back to the import.

    The position stays where it is until the next geometry import, which then
    owns it again. Returns the source it had, or None when there is no such
    object.
    """
    table = {"rack": "rack", "device": "device"}[kind]
    row = (await session.execute(text(
        f"SELECT geometry_source FROM {table} WHERE id = CAST(:id AS uuid)"),
        {"id": object_id})).mappings().first()
    if row is None:
        return None
    await session.execute(text(
        f"UPDATE {table} SET geometry_source = 'import' WHERE id = CAST(:id AS uuid)"),
        {"id": object_id})
    return row["geometry_source"] or "import"
