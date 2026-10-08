"""Rack, room and datacenter queries, including the rack elevation.

The elevation is one query on purpose. A rack view that needs 42 follow-up
requests is the difference between a page that renders in 100 ms and one that
takes four seconds on a control-room laptop.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# A rack's load is what its rack PDUs meter at their inputs - the standard
# reading, and the only one that includes everything plugged in. Summing every
# device instead counted each server twice: once as itself and again inside the
# PDU that feeds it (R2-01 read 32.5 kW for a 16.5 kW rack). The devices' own
# draw is the fallback for a rack with no PDU reporting.
_RACK_LOAD_W = (
    "COALESCE(NULLIF(sum(ds.power_w) FILTER ("
    "WHERE d.device_type IN ('pdu', 'floor_pdu')), 0),"
    " sum(ds.power_w) FILTER (WHERE d.device_type NOT IN ('pdu', 'floor_pdu')), 0)")

_RACK_SUMMARY = f"""
    SELECT r.id::text, r.name, r.u_height, r.rated_power_kw,
           rr.name AS row_name,
           -- Where the rack stands, in words the name only encodes: "R2-04"
           -- is row 2, fourth position. The page spells it out.
           rr.ordinal AS row_ordinal,
           r.ordinal  AS position,
           rm.id::text AS room_id, rm.name AS room_name,
           dc.code AS datacenter_code,
           count(d.id) FILTER (WHERE d.id IS NOT NULL)                AS device_count,
           count(*)    FILTER (WHERE ds.status = 'ONLINE')            AS online_count,
           count(*)    FILTER (WHERE ds.status = 'OFFLINE')           AS offline_count,
           {_RACK_LOAD_W} / 1000.0                                    AS load_kw,
           CASE WHEN r.rated_power_kw > 0
                THEN 100.0 * {_RACK_LOAD_W} / 1000.0 / r.rated_power_kw
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
    "           r.floor_x, r.floor_y, r.facing, r.width_m AS rack_w, r.depth_m AS rack_d,\n"
    "           rr.ordinal AS row_ordinal, rr.cold_aisle, rr.hot_aisle,",
) + """
     WHERE rm.id = CAST(:room_id AS uuid) AND r.floor_x IS NOT NULL
""" + _GROUP_BY + (", r.floor_x, r.floor_y, r.facing, r.width_m, r.depth_m,"
                   " rr.cold_aisle, rr.hot_aisle")


async def floorplan_racks(session: AsyncSession, room_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text(_FLOORPLAN_RACKS),
                                  {"room_id": room_id})).mappings().all()
    return [dict(r) for r in rows]


async def floorplan_equipment(session: AsyncSession,
                              room_id: str) -> list[dict[str, Any]]:
    """Everything in the room that is not in a rack: floor-standing plant and
    the instruments on its walls and pipes.

    Each row carries its stored geometry (migration 0099) - room coordinate,
    footprint, facing, mount - when the import supplied it. A row without a
    coordinate is still returned, so a CRAH the source never placed is listed
    rather than hidden: a plan that omitted it would show the load and hide
    what cools it.
    """
    rows = (await session.execute(text("""
        SELECT d.id::text, d.name, d.device_type::text AS device_type,
               COALESCE(ds.status::text, 'UNKNOWN')     AS status,
               COALESCE(ds.max_severity::text, 'CLEAR') AS max_severity,
               ds.power_w, ds.inlet_temp_c,
               (ds.metrics->'supply_air_temp'->>'v')::float AS supply_c,
               (ds.metrics->'return_air_temp'->>'v')::float AS return_c,
               (ds.metrics->'ambient_temperature'->>'v')::float AS temp_c,
               COALESCE(ds.humidity_pct,
                        (ds.metrics->'relative_humidity'->>'v')::float) AS rh_pct,
               d.floor_x, d.floor_y, d.mount, d.rotation_deg,
               d.footprint_w_m, d.footprint_d_m, d.height_m, d.mount_height_m,
               d.footprint_basis
          FROM device d
          LEFT JOIN device_state ds ON ds.device_id = d.id
         WHERE d.room_id = CAST(:room_id AS uuid)
           AND d.rack_id IS NULL
           AND d.lifecycle <> 'decommissioned'
         ORDER BY d.device_type, d.name
    """), {"room_id": room_id})).mappings().all()
    return [dict(r) for r in rows]


#: Share of nameplate budgeted per device when its model carries no budget of
#: its own. A PSU nameplate is the supply's ceiling, not the server's draw -
#: real draw is commonly a third to a half of it - so planning tools budget a
#: derated figure. 0.6 is a common planning default, not a standard; a site
#: sets its own per model with `model.attributes.budget_w`.
BUDGET_DERATE = 0.6


async def room_rack_power_plan(session: AsyncSession,
                               room_id: str) -> dict[str, dict[str, Any]]:
    """Allocated and reserved power per rack in the room (docs/27 §8a).

    Allocated: the budget of every device physically in the rack (installed,
    in service or in maintenance) - the model's `budget_w` when set, else its
    nameplate derated. Rack PDUs and sensors distribute or measure power and
    are not loads. `planned` devices are left out: a planned device standing in
    a rack is a reservation's placeholder, and counting it here would count
    the reservation twice.

    Reserved: power held for planned work on the rack, status `held`.
    """
    rows = (await session.execute(text("""
        WITH alloc AS (
            SELECT d.rack_id,
                   -- The cast matters: a bare parameter multiplied into an
                   -- integer column is typed integer, and 0.6 arrived as 0 -
                   -- every rack read zero allocated on the live estate.
                   sum(COALESCE(NULLIF(m.attributes->>'budget_w','')::float,
                                m.rated_power_w * CAST(:derate AS float8))) / 1000.0
                       AS allocated_kw,
                   count(*) FILTER (WHERE NULLIF(m.attributes->>'budget_w','') IS NULL
                                      AND m.rated_power_w IS NOT NULL)  AS derated,
                   count(*) FILTER (WHERE m.rated_power_w IS NULL
                                      AND NULLIF(m.attributes->>'budget_w','') IS NULL) AS unrated
              FROM device d
              JOIN rack r      ON r.id = d.rack_id
              JOIN rack_row rr ON rr.id = r.row_id
              LEFT JOIN model m ON m.id = d.model_id
             WHERE rr.room_id = CAST(:room_id AS uuid)
               AND d.lifecycle IN ('installed', 'in_service', 'maintenance')
               AND d.device_type::text NOT IN ('pdu', 'sensor')
             GROUP BY d.rack_id
        ), held AS (
            SELECT cr.rack_id, sum(cr.power_kw) AS reserved_kw
              FROM capacity_reservation cr
              JOIN rack r      ON r.id = cr.rack_id
              JOIN rack_row rr ON rr.id = r.row_id
             WHERE rr.room_id = CAST(:room_id AS uuid) AND cr.status = 'held'
             GROUP BY cr.rack_id
        )
        SELECT COALESCE(a.rack_id, h.rack_id)::text AS rack_id,
               a.allocated_kw, COALESCE(a.derated, 0) AS derated,
               COALESCE(a.unrated, 0) AS unrated, h.reserved_kw
          FROM alloc a FULL JOIN held h ON h.rack_id = a.rack_id
    """), {"room_id": room_id, "derate": BUDGET_DERATE})).mappings().all()
    return {r["rack_id"]: dict(r) for r in rows}


async def room_geometry(session: AsyncSession, room_id: str) -> dict[str, Any] | None:
    """The room as drawn: size, level, and where it stands in its building."""
    row = (await session.execute(text("""
        SELECT rm.id::text, rm.name AS room_name, dc.id::text AS datacenter_id,
               dc.code AS datacenter_code, rm.floor AS level, rm.room_class,
               rm.width_m, rm.depth_m, rm.origin_x_m, rm.origin_y_m,
               rm.rotation_deg, rm.level_elevation_m, rm.geometry_source,
               rm.attributes->>'containment' AS containment,
               rm.ashrae_class
          FROM room rm
          JOIN datacenter dc ON dc.id = rm.datacenter_id
         WHERE rm.id = CAST(:room_id AS uuid)
    """), {"room_id": room_id})).mappings().first()
    return dict(row) if row else None


async def room_aisles(session: AsyncSession, room_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT name, kind, y_m, width_m, between_rows, contained
          FROM aisle
         WHERE room_id = CAST(:room_id AS uuid)
         ORDER BY y_m
    """), {"room_id": room_id})).mappings().all()
    return [dict(r) for r in rows]


async def site_building(session: AsyncSession,
                        datacenter_id: str) -> dict[str, Any] | None:
    """A site as a building: its levels and every room placed on one, with the
    counts and worst condition a building view colours a room by."""
    dc = (await session.execute(text("""
        SELECT id::text, code, name, attributes->'building' AS building
          FROM datacenter WHERE id = CAST(:dc AS uuid)
    """), {"dc": datacenter_id})).mappings().first()
    if dc is None:
        return None
    rooms = (await session.execute(text("""
        WITH placed AS (
            -- A device's room is its rack's room, or its own when it stands on
            -- the floor - the same rule the alarm roll-up uses.
            SELECT d.id, COALESCE(rr.room_id, d.room_id) AS room_id
              FROM device d
              LEFT JOIN rack r      ON r.id = d.rack_id
              LEFT JOIN rack_row rr ON rr.id = r.row_id
             WHERE d.lifecycle <> 'decommissioned'
        )
        SELECT rm.id::text, rm.name, rm.floor AS level, rm.room_class,
               rm.width_m, rm.depth_m, rm.origin_x_m, rm.origin_y_m,
               rm.rotation_deg, rm.level_elevation_m,
               (SELECT count(*) FROM rack r JOIN rack_row rr ON rr.id = r.row_id
                 WHERE rr.room_id = rm.id)                    AS rack_count,
               count(p.id)                                    AS device_count,
               COALESCE(max(ds.max_severity)::text, 'CLEAR')  AS max_severity
          FROM room rm
          LEFT JOIN placed p        ON p.room_id = rm.id
          LEFT JOIN device_state ds ON ds.device_id = p.id
         WHERE rm.datacenter_id = CAST(:dc AS uuid)
         GROUP BY rm.id
         ORDER BY rm.level_elevation_m NULLS LAST, rm.origin_x_m NULLS LAST, rm.name
    """), {"dc": datacenter_id})).mappings().all()
    return {**dict(dc), "rooms": [dict(r) for r in rooms]}


async def site_blocks(session: AsyncSession, datacenter_id: str
                      ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every placed rack and every placed floor-standing unit at a site, as
    blocks: position, footprint and worst condition, keyed by room.

    The building view draws a whole site at once and must not cost one room
    query per room; this is two reads for the lot, with none of the per-rack
    power or thermal detail the room view carries.
    """
    racks = (await session.execute(text("""
        SELECT r.id::text, r.name, rr.room_id::text AS room_id,
               r.floor_x, r.floor_y, r.width_m, r.depth_m,
               COALESCE(max(ds.max_severity)::text, 'CLEAR') AS max_severity
          FROM rack r
          JOIN rack_row rr ON rr.id = r.row_id
          JOIN room rm     ON rm.id = rr.room_id
          LEFT JOIN device d        ON d.rack_id = r.id AND d.lifecycle <> 'decommissioned'
          LEFT JOIN device_state ds ON ds.device_id = d.id
         WHERE rm.datacenter_id = CAST(:dc AS uuid) AND r.floor_x IS NOT NULL
         GROUP BY r.id, rr.room_id
    """), {"dc": datacenter_id})).mappings().all()
    units = (await session.execute(text("""
        SELECT d.id::text, d.name, d.device_type::text AS device_type, d.room_id::text AS room_id,
               d.floor_x, d.floor_y, d.footprint_w_m, d.footprint_d_m, d.height_m, d.rotation_deg,
               COALESCE(ds.max_severity::text, 'CLEAR') AS max_severity
          FROM device d
          JOIN room rm ON rm.id = d.room_id
          LEFT JOIN device_state ds ON ds.device_id = d.id
         WHERE rm.datacenter_id = CAST(:dc AS uuid)
           AND d.rack_id IS NULL AND d.lifecycle <> 'decommissioned'
           AND d.floor_x IS NOT NULL AND d.footprint_w_m IS NOT NULL AND d.footprint_d_m IS NOT NULL
    """), {"dc": datacenter_id})).mappings().all()
    return [dict(r) for r in racks], [dict(u) for u in units]


async def room_rack_devices(session: AsyncSession, room_id: str) -> list[dict[str, Any]]:
    """Every live device in the room's racks, with its slot and state."""
    rows = (await session.execute(text("""
        SELECT d.id::text, d.name, d.device_type::text AS device_type,
               d.rack_id::text AS rack_id, d.u_start, d.u_height, d.mount,
               d.mount_height_m,
               COALESCE(ds.status::text, 'UNKNOWN')     AS status,
               COALESCE(ds.max_severity::text, 'CLEAR') AS max_severity,
               ds.power_w, ds.inlet_temp_c,
               -- The air at the device, whatever it calls it: a server reports
               -- its inlet, a door probe or a strip's probe the ambient air at
               -- its height. One column, so the rack's vertical gradient can be
               -- drawn from every reading in it.
               COALESCE(ds.inlet_temp_c,
                        (ds.metrics->'ambient_temperature'->>'v')::float) AS temp_c,
               COALESCE(ds.humidity_pct,
                        (ds.metrics->'relative_humidity'->>'v')::float)  AS rh_pct,
               -- Only servers report their exhaust; the rear face of the rack
               -- and every rise-based index are drawn from it.
               (ds.metrics->'exhaust_temperature'->>'v')::float          AS exhaust_c
          FROM device d
          JOIN rack r      ON r.id = d.rack_id
          JOIN rack_row rr ON rr.id = r.row_id
          LEFT JOIN device_state ds ON ds.device_id = d.id
         WHERE rr.room_id = CAST(:room_id AS uuid)
           AND d.lifecycle <> 'decommissioned'
         ORDER BY d.rack_id, d.u_start NULLS FIRST
    """), {"room_id": room_id})).mappings().all()
    return [dict(r) for r in rows]
