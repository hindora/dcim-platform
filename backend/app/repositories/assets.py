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

from app.repositories.contracts import EXPIRING_DAYS

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

    warranty = (await session.execute(text("""
        SELECT count(*) FILTER (WHERE warranty_expires IS NULL)        AS unknown,
               count(*) FILTER (WHERE warranty_expires < CURRENT_DATE) AS expired,
               count(*) FILTER (WHERE warranty_expires >= CURRENT_DATE
                                  AND warranty_expires <= CURRENT_DATE + CAST(:expiring AS integer))
                                                                        AS expiring,
               count(*) FILTER (WHERE warranty_expires > CURRENT_DATE + CAST(:expiring AS integer))
                                                                        AS active
        FROM device
        WHERE lifecycle NOT IN ('decommissioned', 'retired')
    """), {"expiring": EXPIRING_DAYS})).mappings().one()

    contracts = (await session.execute(text("""
        SELECT count(*)                                                 AS total,
               count(*) FILTER (WHERE end_date < CURRENT_DATE)          AS expired,
               count(*) FILTER (WHERE end_date >= CURRENT_DATE
                                  AND end_date <= CURRENT_DATE + CAST(:expiring AS integer))
                                                                        AS expiring
        FROM support_contract
    """), {"expiring": EXPIRING_DAYS})).mappings().one()

    stock = (await session.execute(text("""
        SELECT count(*) FILTER (WHERE reorder_at IS NOT NULL
                                  AND on_hand <= reorder_at) AS below_reorder,
               count(*)                                      AS stock_lines
        FROM part_stock
    """))).mappings().one()

    reservations = (await session.execute(text("""
        SELECT count(*) FILTER (WHERE status = 'held')               AS held,
               count(*) FILTER (WHERE status = 'held'
                                  AND expires_at < CURRENT_DATE)     AS overdue
        FROM capacity_reservation
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
        # Present now that migration 0047 gives them something to count. Before
        # it they were ABSENT rather than zero - a tile reading "0 expiring"
        # with no contract table is a statement an operator would act on.
        "warranty": dict(warranty),
        "contracts": dict(contracts),
        "expiring_days": EXPIRING_DAYS,
        "stock": dict(stock),
        "reservations": dict(reservations),
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


# ------------------------------------------------------------------ charts

#: How full a rack is, in bands somebody would act on rather than even tenths.
#: "Empty" and "over 90" are the two that mean something on their own: one is
#: where the next install goes, the other is a rack that cannot take another
#: full-depth server whatever the total free U says.
_FILL_BANDS = ("Empty", "1-25%", "26-50%", "51-75%", "76-90%", "Over 90%")


async def charts(session: AsyncSession) -> dict[str, Any]:
    """Composition and capacity, in one call.

    One round trip because the whole section renders together - three loading
    states stacked on one panel reads as a page that cannot make up its mind.
    """
    by_type = (await session.execute(text("""
        SELECT d.device_type AS key,
               COALESCE(dt.display_name, d.device_type) AS label,
               COALESCE(dt.category, 'unclassified') AS category,
               count(*) AS n
        FROM device d
        LEFT JOIN device_type dt ON dt.code = d.device_type
        GROUP BY d.device_type, dt.display_name, dt.category
        ORDER BY n DESC
    """))).mappings().all()

    by_vendor = (await session.execute(text("""
        SELECT COALESCE(v.name, 'Unrecorded') AS label, count(*) AS n
        FROM device d
        LEFT JOIN vendor v ON v.id = d.vendor_id
        GROUP BY 1
        ORDER BY n DESC
    """))).mappings().all()

    by_lifecycle = (await session.execute(text("""
        SELECT lifecycle::text AS key, count(*) AS n
        FROM device GROUP BY 1
    """))).mappings().all()
    counted = {r["key"]: r["n"] for r in by_lifecycle}

    # Rack space. `used` counts what is INSTALLED; `held` counts planned rows,
    # which occupy the units without being equipment yet - conflating them
    # would report space as free that nobody may take.
    space = (await session.execute(text("""
        SELECT
          (SELECT count(*) FROM rack)                             AS racks,
          (SELECT COALESCE(sum(u_height), 0) FROM rack)           AS u_total,
          (SELECT COALESCE(sum(d.u_height), 0) FROM device d
             WHERE d.rack_id IS NOT NULL AND d.u_start IS NOT NULL
               AND d.lifecycle::text = ANY(:live))                AS u_used,
          (SELECT COALESCE(sum(d.u_height), 0) FROM device d
             WHERE d.rack_id IS NOT NULL AND d.u_start IS NOT NULL
               AND d.lifecycle::text = 'planned')                 AS u_held
    """), {"live": list(LIVE_LIFECYCLES)})).mappings().one()

    # The distribution behind that single number. A mean of 25% across 44 racks
    # says nothing about whether there is one contiguous cabinet to fill or
    # forty part-used ones - which is the only version of the question anybody
    # plans an install against.
    fill = (await session.execute(text("""
        WITH per_rack AS (
            SELECT r.id, r.u_height,
                   COALESCE(sum(d.u_height) FILTER (
                       WHERE d.u_start IS NOT NULL), 0) AS used
            FROM rack r
            LEFT JOIN device d ON d.rack_id = r.id
            GROUP BY r.id, r.u_height
        ), banded AS (
            SELECT CASE
                     WHEN used = 0 THEN 'Empty'
                     WHEN used * 100 / NULLIF(u_height, 0) <= 25 THEN '1-25%'
                     WHEN used * 100 / NULLIF(u_height, 0) <= 50 THEN '26-50%'
                     WHEN used * 100 / NULLIF(u_height, 0) <= 75 THEN '51-75%'
                     WHEN used * 100 / NULLIF(u_height, 0) <= 90 THEN '76-90%'
                     ELSE 'Over 90%'
                   END AS band
            FROM per_rack
        )
        SELECT band, count(*) AS n FROM banded GROUP BY band
    """))).mappings().all()
    banded = {r["band"]: r["n"] for r in fill}

    # Floor space, measured as rack POSITIONS drawn versus racks installed.
    # designed_racks is the denominator the room was laid out with; installed
    # rack count cannot provide it, and floor area alone says nothing about how
    # much of a room is actually usable for equipment.
    floor = (await session.execute(text("""
        SELECT
          count(*)                                        AS rooms,
          COALESCE(sum(rm.designed_racks), 0)             AS designed,
          COALESCE(sum(rm.width_m * rm.depth_m), 0)::float AS area_m2,
          (SELECT count(*) FROM rack)                     AS installed
        FROM room rm
        WHERE rm.designed_racks IS NOT NULL
    """))).mappings().one()

    # WHEN cover lapses, which is a budget question rather than a status one.
    # Quarters, because that is the granularity a renewal is planned at, and
    # only for two years out - past that "Later" is the honest bucket, since
    # nobody plans a refresh from a chart four years ahead.
    #
    # Decommissioned and retired assets are excluded: cover on kit that has
    # left the estate is not a renewal anybody has to fund.
    runway = (await session.execute(text("""
        SELECT CASE
                 WHEN warranty_expires < CURRENT_DATE THEN 'Expired'
                 WHEN warranty_expires <= CURRENT_DATE + 730
                   THEN to_char(warranty_expires, 'YYYY "Q"Q')
                 ELSE 'Beyond 2 years'
               END AS bucket,
               CASE
                 WHEN warranty_expires < CURRENT_DATE THEN 0
                 WHEN warranty_expires <= CURRENT_DATE + 730 THEN 1
                 ELSE 2
               END AS band,
               min(warranty_expires) AS first_lapse,
               count(*) AS n
        FROM device
        WHERE warranty_expires IS NOT NULL
          AND lifecycle NOT IN ('decommissioned', 'retired')
        GROUP BY 1, 2
        ORDER BY band, first_lapse
    """))).mappings().all()

    # Cover as four states, on the same threshold and the same live-estate
    # scope the runway uses - so the two charts beside each other cannot
    # disagree about how many assets there are to cover.
    cover = (await session.execute(text("""
        SELECT count(*) FILTER (WHERE warranty_expires IS NULL)        AS unknown,
               count(*) FILTER (WHERE warranty_expires < CURRENT_DATE) AS expired,
               count(*) FILTER (WHERE warranty_expires >= CURRENT_DATE
                 AND warranty_expires <= CURRENT_DATE + CAST(:expiring AS integer))
                                                                       AS expiring,
               count(*) FILTER (WHERE warranty_expires
                 > CURRENT_DATE + CAST(:expiring AS integer))          AS active
        FROM device
        WHERE lifecycle NOT IN ('decommissioned', 'retired')
    """), {"expiring": EXPIRING_DAYS})).mappings().one()

    # How much of the estate carries each field. The point is not the fields
    # that are full - it is the ones that are not, because every empty column
    # here is a chart or a filter somebody expects to work and finds blank.
    completeness = (await session.execute(text("""
        SELECT count(*)                                              AS total,
               count(serial_number)                                  AS serial_number,
               count(asset_tag)                                      AS asset_tag,
               count(owner_group)                                    AS owner_group,
               count(cost_centre)                                    AS cost_centre,
               count(warranty_expires)                               AS warranty_expires,
               count(purchase_date)                                  AS purchase_date,
               count(install_date)                                   AS install_date,
               count(supplier_id)                                    AS supplier_id,
               count(*) FILTER (WHERE rack_id IS NOT NULL)           AS placement
        FROM device
        WHERE lifecycle NOT IN ('decommissioned', 'retired')
    """))).mappings().one()
    fields = [
        ("Serial number", "serial_number"),
        ("Asset tag", "asset_tag"),
        ("Placement", "placement"),
        ("Owner", "owner_group"),
        ("Cover", "warranty_expires"),
        ("Supplier", "supplier_id"),
        ("Cost centre", "cost_centre"),
        ("Purchase date", "purchase_date"),
        ("Install date", "install_date"),
    ]

    return {
        "by_type": [dict(r) for r in by_type],
        "by_vendor": [dict(r) for r in by_vendor],
        "by_lifecycle": [{"key": k, "n": counted.get(k, 0)}
                         for k in ALL_LIFECYCLES],
        "rack_space": {
            **dict(space),
            "u_free": max(0, space["u_total"] - space["u_used"] - space["u_held"]),
        },
        "rack_fill": [{"band": b, "n": banded.get(b, 0)} for b in _FILL_BANDS],
        "floor_space": {
            **dict(floor),
            "free": max(0, floor["designed"] - floor["installed"]),
        },
        "cover_state": dict(cover),
        "warranty_runway": [
            {"bucket": r["bucket"], "n": r["n"], "band": r["band"]}
            for r in runway
        ],
        "completeness": [
            {"label": label, "filled": completeness[key],
             "total": completeness["total"]}
            for label, key in fields
        ],
    }
