"""Power chain data: hop state, and load for gear that does not report its own."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Every device on the power layer, with whatever load it reports.
#
# load_pct and power_w come from device_state, which the ingest worker keeps as
# the hot set. Note who is missing: on this fleet the rack PDUs and RPPs report
# neither, so their load has to be derived from what they feed - see
# derived_load_w below.
_HOPS = text("""
    SELECT d.id::text                                AS device_id,
           d.name, d.device_type::text               AS device_type,
           COALESCE(ds.status::text, 'UNKNOWN')      AS status,
           COALESCE(ds.max_severity::text, 'CLEAR')  AS max_severity,
           ds.power_w,
           -- The hot set stores {v, q, t} per metric, not a bare number,
           -- so this reaches through to the value.
           (ds.metrics -> 'load_pct' ->> 'v')::float AS load_pct
      FROM device d
      LEFT JOIN device_state ds ON ds.device_id = d.id
     WHERE d.lifecycle <> 'decommissioned'
""")


async def hop_states(session: AsyncSession) -> dict[str, dict[str, Any]]:
    rows = (await session.execute(_HOPS)).mappings().all()
    return {r["device_id"]: dict(r) for r in rows}


async def derived_load_w(session: AsyncSession) -> dict[str, float]:
    """Load for gear that reports none, as the sum of what it feeds.

    One level deep, then rolled up in Python, because a rack PDU's load is the
    sum of the servers on it and an RPP's is the sum of those PDUs. Recursing in
    SQL over a graph with dual feeds would double-count every dual-corded
    server - it appears under both its A and its B feeder.

    So this returns the immediate downstream measured draw only, and the caller
    divides it across the feeders that share a load. Half of a dual-corded
    server's draw is attributed to each side, which is what the metering on a
    real 2N pair shows: each side carries roughly half until one of them fails.
    """
    rows = (await session.execute(text("""
        SELECT c.a_device_id::text AS feeder,
               c.b_device_id::text AS load_dev,
               ds.power_w
          FROM (SELECT DISTINCT a_device_id, b_device_id
                  FROM connection
                 WHERE layer = CAST('power' AS layer_t)
                   AND admin_state = 'enabled') c
          JOIN device_state ds ON ds.device_id = c.b_device_id
         WHERE ds.power_w IS NOT NULL
    """))).mappings().all()

    # How many feeders share each load, so a dual-fed server is not counted
    # once per side.
    feeders_per_load: dict[str, int] = {}
    for r in rows:
        feeders_per_load[r["load_dev"]] = feeders_per_load.get(r["load_dev"], 0) + 1

    out: dict[str, float] = {}
    for r in rows:
        share = float(r["power_w"]) / max(1, feeders_per_load[r["load_dev"]])
        out[r["feeder"]] = out.get(r["feeder"], 0.0) + share
    return out


async def power_edges(session: AsyncSession) -> list[dict[str, Any]]:
    """Distinct feeder -> load pairs with their side.

    DISTINCT because the graph carries one edge per conductor - seven between a
    UPS and an RPP - and for a chain view they are one relationship.
    """
    rows = (await session.execute(text("""
        SELECT DISTINCT a_device_id::text AS feeder,
                        b_device_id::text AS load_dev,
                        redundancy_side
          FROM connection
         WHERE layer = CAST('power' AS layer_t) AND admin_state = 'enabled'
    """))).mappings().all()
    return [dict(r) for r in rows]


async def phase_imbalance(session: AsyncSession,
                          scope_sql: str = "") -> list[dict[str, Any]]:
    """Latest reported phase imbalance, per device.

    Taken from what the gear reports rather than computed from branch-circuit
    currents. The circuit currents on this fleet are per CIRCUIT (Ckt01..Ckt42
    on a panelboard), not per phase, and mapping circuit numbers to phases needs
    the panel's pole layout - which is a real convention but is not in this
    data. Inventing it would produce a confident imbalance figure with nothing
    behind it.
    """
    rows = (await session.execute(text(f"""
        SELECT DISTINCT ON (d.id)
               d.id::text AS device_id, d.name, d.device_type::text AS device_type,
               t.value AS imbalance_pct, t.ts
          FROM telemetry_sample t
          JOIN metric m ON m.id = t.metric_id
          JOIN device d ON d.id = t.device_id
         WHERE m.key = 'phase_imbalance_pct'
           AND t.ts > now() - interval '1 hour'
           {scope_sql}
         ORDER BY d.id, t.ts DESC
    """))).mappings().all()
    return [dict(r) for r in rows]


async def source_devices(session: AsyncSession) -> set[str]:
    """Devices nothing feeds: the utility feeds and generators.

    Derived rather than typed, so a site that feeds its switchgear from
    something this codebase has never heard of still works.
    """
    rows = (await session.execute(text("""
        SELECT d.id::text
          FROM device d
         WHERE EXISTS (SELECT 1 FROM connection c
                        WHERE c.layer = CAST('power' AS layer_t)
                          AND c.a_device_id = d.id)
           AND NOT EXISTS (SELECT 1 FROM connection c
                            WHERE c.layer = CAST('power' AS layer_t)
                              AND c.b_device_id = d.id)
    """))).scalars().all()
    return set(rows)
