"""Asset-view queries over the device model.

There is no `assets` table and there will not be one: `device` is the asset.
See `docs/19-asset-inventory-review.md` B1 for the argument and
`docs/20-asset-data-model.md` §1 for the rule. Everything here reads the same
rows the rest of the platform reads, asking a different question of them -
what do we own, where is it, and what do we not know about it.

Phase 1 asks only questions the current schema can answer. Warranty, support
contracts, maintenance windows and parts arrive with migrations 0046-0049, and
their blocks are absent from the summary rather than present and reading zero:
a tile that says "0 expiring" when no contract table exists is a lie an
operator would act on.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Lifecycle states that mean "this is part of the estate right now". Used as
# the denominator for placement and capacity, never for the asset COUNT - a
# planned device is an asset you own on paper and have not racked, and the
# inventory is wrong if it hides it.
LIVE_LIFECYCLES = ("in_service", "maintenance", "installed")

# Every state the enum can hold, in the enum's own order. Enumerated rather than
# discovered from the rows: a state with no devices must report 0 rather than be
# absent, or the landing page silently drops a column the day the last
# decommissioned device is purged.
ALL_LIFECYCLES = ("planned", "in_stock", "installed", "in_service",
                  "maintenance", "decommissioned", "retired")


async def summary(session: AsyncSession) -> dict[str, Any]:
    """Every count behind the asset landing page, in four queries.

    Four rather than one: they aggregate over different tables at different
    grains, and a single query joining device to rack to room to datacenter
    would multiply rows before counting them - the classic way an estate
    summary reports 44 racks as 664.
    """
    lifecycle_rows = (await session.execute(text("""
        SELECT d.lifecycle::text AS state, count(*) AS n
        FROM device d
        GROUP BY d.lifecycle
    """))).mappings().all()
    by_state = {r["state"]: r["n"] for r in lifecycle_rows}

    identity = (await session.execute(text("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE serial_number IS NOT NULL) AS with_serial,
               count(*) FILTER (WHERE asset_tag IS NOT NULL)     AS with_asset_tag,
               count(*) FILTER (WHERE serial_number IS NULL
                                  AND asset_tag IS NULL)         AS unidentified
        FROM device
    """))).mappings().one()

    # u_used sums the DEVICE's u_height, not the model's: what is consumed in
    # the rack is what was installed, and a 2U server in a slot booked as 1U is
    # a data error worth seeing rather than smoothing over.
    estate = (await session.execute(text("""
        SELECT
          (SELECT count(*) FROM datacenter)                      AS datacenters,
          (SELECT count(*) FROM room)                            AS rooms,
          (SELECT count(*) FROM rack)                            AS racks,
          (SELECT COALESCE(sum(u_height), 0) FROM rack)          AS u_total,
          (SELECT COALESCE(sum(d.u_height), 0) FROM device d
             WHERE d.rack_id IS NOT NULL AND d.u_start IS NOT NULL
               AND d.lifecycle::text = ANY(:live))               AS u_used,
          (SELECT COALESCE(sum(d.u_height), 0) FROM device d
             WHERE d.rack_id IS NOT NULL AND d.u_start IS NOT NULL
               AND d.lifecycle::text = 'planned')                AS u_reserved
    """), {"live": list(LIVE_LIFECYCLES)})).mappings().one()

    category_rows = (await session.execute(text("""
        SELECT COALESCE(dt.category, 'unclassified') AS category, count(*) AS n
        FROM device d
        LEFT JOIN device_type dt ON dt.code = d.device_type
        GROUP BY 1
        ORDER BY n DESC
    """))).mappings().all()

    discovery = (await session.execute(text("""
        SELECT count(*) FILTER (WHERE status = 'new')            AS new_candidates,
               count(*) FILTER (WHERE status = 'new'
                                  AND matched_device_id IS NULL) AS unmatched
        FROM discovery_candidate
    """))).mappings().one()

    return {
        "totals": {
            "assets": identity["total"],
            **{state: by_state.get(state, 0) for state in ALL_LIFECYCLES},
        },
        "identity": {
            "with_serial": identity["with_serial"],
            "with_asset_tag": identity["with_asset_tag"],
            "unidentified": identity["unidentified"],
        },
        "estate": dict(estate),
        "by_category": [dict(r) for r in category_rows],
        "discovery": dict(discovery),
    }


async def device_types(session: AsyncSession) -> list[dict[str, Any]]:
    """The catalog, with how many of each the estate actually holds.

    The count is what makes this usable as a filter: a picker listing 27 types
    when 25 are present and two have never existed here sends people looking
    for rows that cannot be returned.
    """
    rows = (await session.execute(text("""
        SELECT dt.code, dt.display_name, dt.category, dt.is_rack_mounted,
               count(d.id) AS device_count
        FROM device_type dt
        LEFT JOIN device d ON d.device_type = dt.code
        GROUP BY dt.code, dt.display_name, dt.category, dt.is_rack_mounted
        ORDER BY dt.category, dt.display_name
    """))).mappings().all()
    return [dict(r) for r in rows]


async def vendors(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT v.id::text, v.name, count(d.id) AS device_count
        FROM vendor v
        LEFT JOIN device d ON d.vendor_id = v.id
        GROUP BY v.id, v.name
        HAVING count(d.id) > 0
        ORDER BY v.name
    """))).mappings().all()
    return [dict(r) for r in rows]
