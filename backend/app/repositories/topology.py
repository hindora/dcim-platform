"""Topology graph queries.

The graph lives in one table, ``connection``, with a ``layer`` discriminator.
Each layer is a genuinely different graph over the same devices, and they are
not interchangeable:

* ``power`` and ``cooling`` are directed - A feeds B. A cord runs from a PDU
  outlet to a PSU inlet, and a hydronic pipe from a chiller to a CRAH. Which
  end is which is the whole content of the edge.
* ``production`` and ``management`` are ethernet and effectively undirected;
  the a/b ends are just the two ends of a cable.
* ``fieldbus`` is a serial trunk: one gateway fronting many field devices.

Traversal is bidirectional on every layer regardless, because an operator
asking "what is the power topology of this room" wants both the loads in it and
the switchgear feeding it. Direction is preserved in the returned edge so the
client can draw the arrow; it just does not constrain the walk.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.layers import UPSTREAM_COL

# Scope anchors. Each returns a set of device ids to seed the walk from.
#
# The location chain is device -> rack -> rack_row -> room -> datacenter, with
# device.room_id as the fallback for floor-standing plant that sits in no rack
# (chillers, switchgear, UPS). Missing that fallback would silently drop
# exactly the equipment the power and cooling layers are about.
_SEED_SQL = {
    "device": "SELECT CAST(:scope_id AS uuid) AS id",
    "rack": """
        SELECT d.id FROM device d
         WHERE d.rack_id = CAST(:scope_id AS uuid)
           AND d.lifecycle <> 'decommissioned'
    """,
    "room": """
        SELECT d.id FROM device d
         LEFT JOIN rack r      ON r.id = d.rack_id
         LEFT JOIN rack_row rr ON rr.id = r.row_id
         WHERE COALESCE(rr.room_id, d.room_id) = CAST(:scope_id AS uuid)
           AND d.lifecycle <> 'decommissioned'
    """,
    "datacenter": """
        SELECT d.id FROM device d
         LEFT JOIN rack r      ON r.id = d.rack_id
         LEFT JOIN rack_row rr ON rr.id = r.row_id
         LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
         WHERE rm.datacenter_id = CAST(:scope_id AS uuid)
           AND d.lifecycle <> 'decommissioned'
    """,
}


def _nodes_sql(scope_type: str) -> str:
    """Reachable devices within :depth hops of the scope anchor.

    The recursive term walks both directions in ONE branch with an OR rather
    than two UNION branches, because Postgres permits only a single reference
    to the recursive CTE. The OR still uses both (layer, a_device_id) and
    (layer, b_device_id) indexes via a bitmap.

    UNION rather than UNION ALL: the graph has cycles - a dual-fed rack PDU
    reaches its server by two paths - and UNION_ALL would not terminate on
    them.
    """
    return f"""
        WITH RECURSIVE seed AS (
            {_SEED_SQL[scope_type]}
        ),
        reach AS (
            SELECT id AS device_id, 0 AS depth FROM seed
            UNION
            SELECT CASE WHEN c.a_device_id = r.device_id
                        THEN c.b_device_id ELSE c.a_device_id END,
                   r.depth + 1
              FROM reach r
              JOIN connection c
                ON (c.a_device_id = r.device_id OR c.b_device_id = r.device_id)
               AND c.layer = CAST(:layer AS layer_t)
               AND c.admin_state = 'enabled'
             WHERE r.depth < :depth
        ),
        agg AS (
            SELECT device_id, min(depth) AS depth FROM reach GROUP BY device_id
        ),
        deg AS (
            SELECT a.device_id, a.depth,
                   count(c.id) AS degree
              FROM agg a
              LEFT JOIN connection c
                ON (c.a_device_id = a.device_id OR c.b_device_id = a.device_id)
               AND c.layer = CAST(:layer AS layer_t)
               AND c.admin_state = 'enabled'
             GROUP BY a.device_id, a.depth
        )
        SELECT d.id::text                                AS id,
               d.name,
               d.device_type::text                       AS device_type,
               g.depth,
               g.degree,
               count(*) OVER ()                          AS total_reached,
               COALESCE(ds.status::text, 'UNKNOWN')      AS status,
               COALESCE(ds.max_severity::text, 'CLEAR')  AS max_severity,
               -- The typed hot columns only. A power graph wants load and a
               -- cooling graph wants inlet temperature; neither wants every
               -- sample the device has ever produced.
               ds.power_w, ds.inlet_temp_c, ds.cpu_util_pct, ds.humidity_pct,
               -- The datasheet rating, not a reading. A one-line diagram
               -- without capacity is a picture: the number an operator needs
               -- beside a live draw is what the thing is built to take.
               d.rated_power_w,
               rm.id::text AS room_id, rm.name AS room_name,
               r.id::text  AS rack_id, r.name AS rack_name,
               dc.id::text AS datacenter_id, dc.code AS datacenter_code
          FROM deg g
          JOIN device d             ON d.id = g.device_id
          LEFT JOIN device_state ds ON ds.device_id = d.id
          LEFT JOIN rack r          ON r.id = d.rack_id
          LEFT JOIN rack_row rr     ON rr.id = r.row_id
          LEFT JOIN room rm         ON rm.id = COALESCE(rr.room_id, d.room_id)
          LEFT JOIN datacenter dc   ON dc.id = rm.datacenter_id
         WHERE d.lifecycle <> 'decommissioned'
         -- The anchor set survives truncation whatever its degree. Dropping
         -- the device you asked about because it has one cable would be
         -- absurd; past that, keep the best-connected nodes, which are the
         -- ones that make a partial graph readable.
         ORDER BY (g.depth = 0) DESC, g.degree DESC, d.name
         LIMIT :cap
    """


# Edges are the subgraph INDUCED by the kept nodes: an edge is returned only
# when both of its ends survived. A dangling edge to a node the client does not
# have is worse than no edge - it renders as a line into empty space.
_EDGES_SQL = """
    SELECT c.id::text                     AS id,
           c.a_device_id::text            AS source,
           c.b_device_id::text            AS target,
           c.layer::text                  AS layer,
           c.link_type,
           c.redundancy_side,
           c.oper_state,
           c.a_termination_type::text     AS a_termination_type,
           c.a_termination_id::text       AS a_termination_id,
           c.b_termination_type::text     AS b_termination_type,
           c.b_termination_id::text       AS b_termination_id
      FROM connection c
     WHERE c.layer = CAST(:layer AS layer_t)
       AND c.admin_state = 'enabled'
       AND c.a_device_id = ANY(CAST(:ids AS uuid[]))
       AND c.b_device_id = ANY(CAST(:ids AS uuid[]))
     ORDER BY c.id
"""

# Termination labels are resolved per type because the id is polymorphic - it
# points at interface, outlet or power_supply depending on the type column, so
# there is no foreign key to join through. An unlabelled termination is not an
# error: 'none' is a real value for gear cabled without port-level detail.
# Outlets and PSUs have no name of their own - they are identified by position
# on the device, which is also how they are labelled on the physical hardware
# and what an operator reads off the bus bar. The connector type comes along
# because C13 and C19 are not interchangeable and picking the wrong one is a
# site visit.
_TERM_LABEL_SQL = {
    "interface": "SELECT id::text, name AS label FROM interface "
                 "WHERE id = ANY(CAST(:ids AS uuid[]))",
    "outlet": "SELECT id::text, 'Out-' || number::text || "
              "COALESCE(' (' || connector || ')', '') AS label FROM outlet "
              "WHERE id = ANY(CAST(:ids AS uuid[]))",
    "psu": "SELECT id::text, 'PSU' || number::text || "
           "COALESCE(' (' || connector || ')', '') AS label FROM power_supply "
           "WHERE id = ANY(CAST(:ids AS uuid[]))",
}


# The same three tables again, but carrying the electrical facts rather than
# just a name. A trace is read by someone about to unplug something: "Out-12"
# tells them which socket, and "C19, 16 A, phase L2" tells them whether the
# cord in their hand fits it and what trips if it does not. Fetched only for
# the terminations on the hops actually returned, which is a few dozen rows
# even when the layer holds a thousand conductors.
_TERM_DETAIL_SQL = {
    "interface": """
        SELECT id::text AS id, name AS label, NULL::text AS connector,
               NULL::numeric AS rated_amps, NULL::text AS phase,
               NULL::text AS branch, speed_bps
          FROM interface WHERE id = ANY(CAST(:ids AS uuid[]))
    """,
    "outlet": """
        SELECT id::text AS id, 'Out-' || number::text AS label, connector,
               rated_amps, phase, branch, NULL::bigint AS speed_bps
          FROM outlet WHERE id = ANY(CAST(:ids AS uuid[]))
    """,
    "psu": """
        SELECT id::text AS id, 'PSU' || number::text AS label, connector,
               NULL::numeric AS rated_amps, NULL::text AS phase,
               NULL::text AS branch, NULL::bigint AS speed_bps,
               rated_watts
          FROM power_supply WHERE id = ANY(CAST(:ids AS uuid[]))
    """,
}


async def termination_details(session: AsyncSession,
                              by_type: dict[str, list[str]]) -> dict[str, dict[str, Any]]:
    """Resolve termination ids to the facts printed on a trace row."""
    out: dict[str, dict[str, Any]] = {}
    for term_type, ids in by_type.items():
        sql = _TERM_DETAIL_SQL.get(term_type)
        if not sql or not ids:
            continue
        rows = (await session.execute(text(sql), {"ids": ids})).mappings().all()
        for r in rows:
            out[r["id"]] = dict(r)
    return out


async def graph_nodes(session: AsyncSession, *, scope_type: str, scope_id: str,
                      layer: str, depth: int, cap: int) -> list[dict[str, Any]]:
    rows = (await session.execute(
        text(_nodes_sql(scope_type)),
        {"scope_id": scope_id, "layer": layer, "depth": depth, "cap": cap},
    )).mappings().all()
    return [dict(r) for r in rows]


async def graph_edges(session: AsyncSession, *, layer: str,
                      device_ids: list[str]) -> list[dict[str, Any]]:
    if not device_ids:
        return []
    rows = (await session.execute(
        text(_EDGES_SQL), {"layer": layer, "ids": device_ids},
    )).mappings().all()
    return [dict(r) for r in rows]


async def termination_labels(session: AsyncSession,
                             by_type: dict[str, list[str]]) -> dict[str, str]:
    """Resolve termination ids to human labels, one query per type."""
    out: dict[str, str] = {}
    for term_type, ids in by_type.items():
        sql = _TERM_LABEL_SQL.get(term_type)
        if not sql or not ids:
            continue
        rows = (await session.execute(text(sql), {"ids": ids})).all()
        for term_id, label in rows:
            out[term_id] = label
    return out


async def graph_version(session: AsyncSession) -> str:
    """A cheap stamp that changes when the graph does.

    Used in the cache key so a re-import invalidates cached graphs without
    anyone having to remember to flush. oper_state is included because a link
    going down changes what the client should draw, and it moves no timestamp
    of its own.
    """
    row = (await session.execute(text("""
        SELECT count(*)                                        AS n,
               COALESCE(max(created_at)::text, '')             AS newest,
               count(*) FILTER (WHERE oper_state = 'down')     AS down
          FROM connection
    """))).first()
    return f"{row.n}:{row.newest}:{row.down}"


async def layer_edges(session: AsyncSession, layer: str) -> list[dict[str, Any]]:
    """Every edge on one layer, normalised so up/down are the real direction.

    Whole-layer rather than a bounded walk: impact analysis has to know whether
    a path from a SOURCE survives, and that is a global question. The largest
    layer here is 7560 rows, which is cheaper to fetch once than to answer with
    a recursive query per candidate device.
    """
    up_col, down_col = UPSTREAM_COL[layer]
    rows = (await session.execute(text(f"""
        SELECT c.{up_col}::text   AS up,
               c.{down_col}::text AS down,
               c.redundancy_side
          FROM connection c
         WHERE c.layer = CAST(:layer AS layer_t)
           AND c.admin_state = 'enabled'
    """), {"layer": layer})).mappings().all()
    return [dict(r) for r in rows]


async def layer_edges_detailed(session: AsyncSession,
                               layer: str) -> list[dict[str, Any]]:
    """Every edge on one layer, normalised upstream -> downstream, with the
    termination at each end.

    ``layer_edges`` above deliberately carries only the three columns impact
    analysis needs, because it is called once per layer for every candidate.
    A trace is asked about one device at a time and has to print what the
    conductor lands on at both ends, so it pays for the wider row.

    Whole-layer for the same reason ``layer_edges`` is: the walk has to reach
    a SOURCE, which is a global question, and the largest layer here is 1080
    rows. A recursive query per trace costs more than one fetch.
    """
    up_col, down_col = UPSTREAM_COL[layer]
    # The termination columns are polymorphic on the a/b ends, so they have to
    # follow whichever end the layer calls upstream. Management is the layer
    # where this matters: the managed device holds the a end there, so its
    # upstream termination is the b one.
    up_side = "a" if up_col == "a_device_id" else "b"
    down_side = "b" if up_side == "a" else "a"
    rows = (await session.execute(text(f"""
        SELECT c.id::text                              AS id,
               c.{up_col}::text                        AS up,
               c.{down_col}::text                      AS down,
               c.link_type,
               c.redundancy_side,
               c.oper_state,
               c.{up_side}_termination_type::text      AS up_termination_type,
               c.{up_side}_termination_id::text        AS up_termination_id,
               c.{down_side}_termination_type::text    AS down_termination_type,
               c.{down_side}_termination_id::text      AS down_termination_id
          FROM connection c
         WHERE c.layer = CAST(:layer AS layer_t)
           AND c.admin_state = 'enabled'
    """), {"layer": layer})).mappings().all()
    return [dict(r) for r in rows]


async def devices_brief(session: AsyncSession,
                        ids: list[str]) -> dict[str, dict[str, Any]]:
    """Just enough about each device to render an impact list."""
    if not ids:
        return {}
    rows = (await session.execute(text("""
        SELECT d.id::text AS id, d.name, d.device_type::text AS device_type,
               COALESCE(ds.status::text, 'UNKNOWN') AS status,
               rm.name AS room_name, r.name AS rack_name
          FROM device d
          LEFT JOIN device_state ds ON ds.device_id = d.id
          LEFT JOIN rack r          ON r.id = d.rack_id
          LEFT JOIN rack_row rr     ON rr.id = r.row_id
          LEFT JOIN room rm         ON rm.id = COALESCE(rr.room_id, d.room_id)
         WHERE d.id = ANY(CAST(:ids AS uuid[]))
    """), {"ids": ids})).mappings().all()
    return {r["id"]: dict(r) for r in rows}
