"""Rack, room and datacenter queries, including the rack elevation.

The elevation is one query on purpose. A rack view that needs 42 follow-up
requests is the difference between a page that renders in 100 ms and one that
takes four seconds on a control-room laptop.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_RACK_SUMMARY = """
    SELECT r.id::text, r.name, r.u_height, r.rated_power_kw,
           rr.name AS row_name,
           rm.id::text AS room_id, rm.name AS room_name,
           dc.code AS datacenter_code,
           count(d.id) FILTER (WHERE d.id IS NOT NULL)                AS device_count,
           count(*)    FILTER (WHERE ds.status = 'ONLINE')            AS online_count,
           count(*)    FILTER (WHERE ds.status = 'OFFLINE')           AS offline_count,
           COALESCE(sum(ds.power_w), 0) / 1000.0                      AS load_kw,
           CASE WHEN r.rated_power_kw > 0
                THEN 100.0 * COALESCE(sum(ds.power_w), 0) / 1000.0 / r.rated_power_kw
           END                                                        AS load_pct,
           max(ds.inlet_temp_c)                                       AS max_inlet_c,
           COALESCE(max(ds.max_severity)::text, 'CLEAR')              AS max_severity,
           r.u_height - COALESCE(sum(d.u_height)
                                 FILTER (WHERE d.u_start IS NOT NULL), 0) AS free_u
    FROM rack r
    JOIN rack_row rr      ON rr.id = r.row_id
    JOIN room rm          ON rm.id = rr.room_id
    JOIN datacenter dc    ON dc.id = rm.datacenter_id
    LEFT JOIN device d    ON d.rack_id = r.id AND d.lifecycle <> 'decommissioned'
    LEFT JOIN device_state ds ON ds.device_id = d.id
"""

# rr.ordinal and r.ordinal are grouped as well as selected: they drive the
# ORDER BY, and Postgres requires every ordered column to be grouped or
# aggregated.
_GROUP_BY = " GROUP BY r.id, rr.name, rr.ordinal, rm.id, rm.name, dc.code"


async def list_racks(session: AsyncSession, *, room_id: str | None = None,
                     datacenter_id: str | None = None,
                     limit: int = 200) -> list[dict[str, Any]]:
    where, params = [], {"limit": limit}
    if room_id:
        where.append("rm.id = CAST(:room_id AS uuid)")
        params["room_id"] = room_id
    if datacenter_id:
        where.append("dc.id = CAST(:datacenter_id AS uuid)")
        params["datacenter_id"] = datacenter_id
    sql = _RACK_SUMMARY + (" WHERE " + " AND ".join(where) if where else "") \
        + _GROUP_BY + " ORDER BY dc.code, rm.name, rr.ordinal, r.ordinal LIMIT :limit"
    rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def get_rack(session: AsyncSession, rack_id: str) -> dict[str, Any] | None:
    sql = _RACK_SUMMARY + " WHERE r.id = CAST(:id AS uuid)" + _GROUP_BY
    row = (await session.execute(text(sql), {"id": rack_id})).mappings().first()
    return dict(row) if row else None


async def rack_devices(session: AsyncSession, rack_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT d.id::text, d.name, d.device_type, d.u_start, d.u_height, d.facing,
               COALESCE(ds.status::text, 'UNKNOWN')      AS status,
               COALESCE(ds.health::text, 'UNKNOWN')      AS health,
               COALESCE(ds.max_severity::text, 'CLEAR')  AS max_severity,
               ds.power_w, ds.inlet_temp_c, ds.cpu_util_pct
        FROM device d
        LEFT JOIN device_state ds ON ds.device_id = d.id
        WHERE d.rack_id = CAST(:id AS uuid) AND d.lifecycle <> 'decommissioned'
        ORDER BY d.u_start DESC NULLS LAST, d.name
    """), {"id": rack_id})).mappings().all()
    return [dict(r) for r in rows]


async def list_datacenters(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT dc.id::text, dc.code, dc.name, dc.city, dc.country,
               count(DISTINCT rm.id) AS room_count,
               count(DISTINCT r.id)  AS rack_count,
               count(DISTINCT d.id)  AS device_count
        FROM datacenter dc
        LEFT JOIN room rm     ON rm.datacenter_id = dc.id
        LEFT JOIN rack_row rr ON rr.room_id = rm.id
        LEFT JOIN rack r      ON r.row_id = rr.id
        LEFT JOIN device d    ON (d.rack_id = r.id OR d.room_id = rm.id)
                                 AND d.lifecycle <> 'decommissioned'
        GROUP BY dc.id ORDER BY dc.code
    """))).mappings().all()
    return [dict(r) for r in rows]


async def list_rooms(session: AsyncSession,
                     datacenter_id: str | None = None) -> list[dict[str, Any]]:
    where = " WHERE rm.datacenter_id = CAST(:dc AS uuid)" if datacenter_id else ""
    rows = (await session.execute(text(f"""
        SELECT rm.id::text, rm.name, rm.floor, rm.room_type,
               dc.code AS datacenter_code, dc.id::text AS datacenter_id,
               count(DISTINCT r.id) AS rack_count,
               count(DISTINCT d.id) AS device_count
        FROM room rm
        JOIN datacenter dc    ON dc.id = rm.datacenter_id
        LEFT JOIN rack_row rr ON rr.room_id = rm.id
        LEFT JOIN rack r      ON r.row_id = rr.id
        LEFT JOIN device d    ON (d.rack_id = r.id OR d.room_id = rm.id)
                                 AND d.lifecycle <> 'decommissioned'
        {where}
        GROUP BY rm.id, dc.code, dc.id ORDER BY dc.code, rm.name
    """), {"dc": datacenter_id} if datacenter_id else {})).mappings().all()
    return [dict(r) for r in rows]


async def list_rows(session: AsyncSession, room_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT rr.id::text, rr.name, rr.ordinal, rr.cold_aisle, rr.hot_aisle,
               count(r.id) AS rack_count
        FROM rack_row rr
        LEFT JOIN rack r ON r.row_id = rr.id
        WHERE rr.room_id = CAST(:room AS uuid)
        GROUP BY rr.id ORDER BY rr.ordinal, rr.name
    """), {"room": room_id})).mappings().all()
    return [dict(r) for r in rows]


def compute_free_blocks(u_height: int, occupied: list[tuple[int, int]]) -> list[dict[str, int]]:
    """Largest contiguous free spans, computed server-side.

    "What is the largest contiguous free block" is the question capacity
    planning actually asks, and deriving it in the browser from a sparse list is
    an off-by-one waiting to happen.
    """
    taken = set()
    for u_start, height in occupied:
        if u_start is None:
            continue
        for u in range(u_start, u_start + max(height, 1)):
            taken.add(u)

    blocks: list[dict[str, int]] = []
    run_start = None
    for u in range(1, u_height + 1):
        if u not in taken:
            if run_start is None:
                run_start = u
        elif run_start is not None:
            blocks.append({"u_start": run_start, "u_height": u - run_start})
            run_start = None
    if run_start is not None:
        blocks.append({"u_start": run_start, "u_height": u_height + 1 - run_start})
    return sorted(blocks, key=lambda b: -b["u_height"])


_FLOORPLAN_RACKS = _RACK_SUMMARY.replace(
    "SELECT r.id::text, r.name, r.u_height, r.rated_power_kw,",
    "SELECT r.id::text, r.name, r.u_height, r.rated_power_kw,\n"
    "           r.floor_x, r.floor_y, r.facing,\n"
    "           rr.ordinal AS row_ordinal, rr.cold_aisle, rr.hot_aisle,",
) + """
     WHERE rm.id = CAST(:room_id AS uuid) AND r.floor_x IS NOT NULL
""" + _GROUP_BY + ", r.floor_x, r.floor_y, r.facing, rr.cold_aisle, rr.hot_aisle"


async def floorplan_racks(session: AsyncSession, room_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text(_FLOORPLAN_RACKS),
                                  {"room_id": room_id})).mappings().all()
    return [dict(r) for r in rows]


async def floorplan_equipment(session: AsyncSession,
                              room_id: str) -> list[dict[str, Any]]:
    """Floor-standing plant in the room.

    The source carries no room coordinate for it - only a position in its
    fleet-wide canvas diagram, which is pixels, not metres - so this returns
    what is in the room without pretending to know where it stands. CRAH units
    especially: a floor plan that omitted them entirely would show the load and
    hide what cools it.
    """
    rows = (await session.execute(text("""
        SELECT d.id::text, d.name, d.device_type::text AS device_type,
               COALESCE(ds.status::text, 'UNKNOWN')     AS status,
               COALESCE(ds.max_severity::text, 'CLEAR') AS max_severity,
               ds.power_w, ds.inlet_temp_c
          FROM device d
          LEFT JOIN device_state ds ON ds.device_id = d.id
         WHERE d.room_id = CAST(:room_id AS uuid)
           AND d.rack_id IS NULL
           AND d.lifecycle <> 'decommissioned'
         ORDER BY d.device_type, d.name
    """), {"room_id": room_id})).mappings().all()
    return [dict(r) for r in rows]
