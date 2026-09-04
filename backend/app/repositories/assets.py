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


async def sites(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id::text, code FROM datacenter ORDER BY code
    """))).mappings().all()
    return [dict(r) for r in rows]


async def rooms_by_site(session: AsyncSession) -> list[dict[str, Any]]:
    """Rooms with their site, so the room picker can follow the site picker."""
    rows = (await session.execute(text("""
        SELECT rm.id::text, rm.name, dc.id::text AS datacenter_id, dc.code AS dc
        FROM room rm
        JOIN datacenter dc ON dc.id = rm.datacenter_id
        ORDER BY dc.code, rm.name
    """))).mappings().all()
    return [dict(r) for r in rows]


async def owner_groups(session: AsyncSession) -> list[str]:
    """The owner vocabulary as it exists on devices - free text, so the
    picker offers what is actually recorded rather than a hoped-for list."""
    rows = (await session.execute(text("""
        SELECT DISTINCT owner_group FROM device
        WHERE owner_group IS NOT NULL ORDER BY 1
    """))).mappings().all()
    return [r["owner_group"] for r in rows]


# ------------------------------------------------------------------ charts

async def charts(session: AsyncSession) -> dict[str, Any]:
    """Composition and capacity, in one call.

    One round trip because the whole section renders together - three loading
    states stacked on one panel reads as a page that cannot make up its mind.
    """
    # One cube for the composition panels: the same devices grouped once by
    # (type, vendor, owner, site). "By type", "By make" and "By owner" are
    # rollups of it, so each panel's filters - site plus another panel's
    # dimension - are client-side sums over rows already in hand rather than
    # extra round trips. All joins are LEFT: an unplaced device has no site
    # (dc is NULL) but must not vanish from the composition of the estate.
    # Decommissioned and retired kit is excluded, as everywhere in this
    # section - what has left the estate is not part of its composition.
    composition = (await session.execute(text("""
        SELECT d.device_type AS type_key,
               COALESCE(dt.display_name, d.device_type) AS type_label,
               COALESCE(v.name, 'Unrecorded') AS vendor,
               COALESCE(d.owner_group, 'Unassigned') AS owner,
               dc.code AS dc,
               count(*) AS n
        FROM device d
        LEFT JOIN device_type dt ON dt.code = d.device_type
        LEFT JOIN vendor v ON v.id = d.vendor_id
        LEFT JOIN rack r ON r.id = d.rack_id
        LEFT JOIN rack_row rr ON rr.id = r.row_id
        LEFT JOIN room rm ON rm.id = COALESCE(rr.room_id, d.room_id)
        LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
        WHERE d.lifecycle NOT IN ('decommissioned', 'retired')
        GROUP BY d.device_type, dt.display_name, v.name, d.owner_group, dc.code
    """))).mappings().all()

    # Its own cube, NOT the composition cube: lifecycle is the one panel that
    # deliberately counts decommissioned and retired kit - "nothing is
    # decommissioned" is worth seeing - while composition excludes what has
    # left the estate. Dimensioned by (type, site) for the panel's filters.
    by_lifecycle = (await session.execute(text("""
        SELECT d.lifecycle::text AS key,
               COALESCE(dt.display_name, d.device_type) AS type_label,
               dc.code AS dc,
               count(*) AS n
        FROM device d
        LEFT JOIN device_type dt ON dt.code = d.device_type
        LEFT JOIN rack r ON r.id = d.rack_id
        LEFT JOIN rack_row rr ON rr.id = r.row_id
        LEFT JOIN room rm ON rm.id = COALESCE(rr.room_id, d.room_id)
        LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
        GROUP BY 1, 2, dc.code
    """))).mappings().all()

    # Rack space. `used` counts what is INSTALLED; `held` counts planned rows,
    # which occupy the units without being equipment yet - conflating them
    # would report space as free that nobody may take. One row per (site,
    # room) - capacity is a per-hall conversation, and every figure here is
    # additive over racks, so the panel's filters are client-side sums.
    space = (await session.execute(text("""
        WITH per_rack AS (
            SELECT r.id, r.u_height, rm.name AS room, dc.code AS dc,
                   COALESCE(sum(d.u_height) FILTER (
                       WHERE d.u_start IS NOT NULL
                         AND d.lifecycle::text = ANY(:live)), 0)   AS used,
                   COALESCE(sum(d.u_height) FILTER (
                       WHERE d.u_start IS NOT NULL
                         AND d.lifecycle::text = 'planned'), 0)    AS held
            FROM rack r
            LEFT JOIN rack_row rr ON rr.id = r.row_id
            LEFT JOIN room rm ON rm.id = rr.room_id
            LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
            LEFT JOIN device d ON d.rack_id = r.id
            GROUP BY r.id, r.u_height, rm.name, dc.code
        )
        SELECT dc, room, count(*) AS racks,
               COALESCE(sum(u_height), 0)::int AS u_total,
               sum(used)::int AS u_used, sum(held)::int AS u_held
        FROM per_rack GROUP BY dc, room
    """), {"live": list(LIVE_LIFECYCLES)})).mappings().all()

    # The distribution behind that single number. A mean of 25% across 44 racks
    # says nothing about whether there is one contiguous cabinet to fill or
    # forty part-used ones - which is the only version of the question anybody
    # plans an install against. Dimensioned by (site, room): a hall of
    # part-used racks and a hall with one empty cabinet look identical
    # estate-wide.
    fill = (await session.execute(text("""
        WITH per_rack AS (
            SELECT r.id, r.u_height, rm.name AS room, dc.code AS dc,
                   COALESCE(sum(d.u_height) FILTER (
                       WHERE d.u_start IS NOT NULL), 0) AS used
            FROM rack r
            LEFT JOIN rack_row rr ON rr.id = r.row_id
            LEFT JOIN room rm ON rm.id = rr.room_id
            LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
            LEFT JOIN device d ON d.rack_id = r.id
            GROUP BY r.id, r.u_height, rm.name, dc.code
        ), banded AS (
            SELECT dc, room,
                   CASE
                     WHEN used = 0 THEN 'Empty'
                     WHEN used * 100 / NULLIF(u_height, 0) <= 25 THEN '1-25%'
                     WHEN used * 100 / NULLIF(u_height, 0) <= 50 THEN '26-50%'
                     WHEN used * 100 / NULLIF(u_height, 0) <= 75 THEN '51-75%'
                     WHEN used * 100 / NULLIF(u_height, 0) <= 90 THEN '76-90%'
                     ELSE 'Over 90%'
                   END AS band
            FROM per_rack
        )
        SELECT dc, room, band, count(*) AS n
        FROM banded GROUP BY dc, room, band
    """))).mappings().all()

    # Floor space, measured as rack POSITIONS drawn versus racks installed.
    # designed_racks is the denominator the room was laid out with; installed
    # rack count cannot provide it, and floor area alone says nothing about how
    # much of a room is actually usable for equipment. One row per room; the
    # estate view is the sum, and a filtered gauge keeps an honest
    # denominator because the denominator lives on the room.
    floor = (await session.execute(text("""
        SELECT dc.code AS dc, rm.name AS room,
               COALESCE(rm.designed_racks, 0)                   AS designed,
               COALESCE(rm.width_m * rm.depth_m, 0)::float      AS area_m2,
               (SELECT count(*) FROM rack r
                  JOIN rack_row rr ON rr.id = r.row_id
                  WHERE rr.room_id = rm.id)                     AS installed
        FROM room rm
        JOIN datacenter dc ON dc.id = rm.datacenter_id
        WHERE rm.designed_racks IS NOT NULL
    """))).mappings().all()

    # WHEN cover lapses, which is a budget question rather than a status one.
    # Quarters, because that is the granularity a renewal is planned at, and
    # only for two years out - past that "Later" is the honest bucket, since
    # nobody plans a refresh from a chart four years ahead.
    #
    # Decommissioned and retired assets are excluded: cover on kit that has
    # left the estate is not a renewal anybody has to fund.
    # The runway rows carry vendor and site because renewal is a per-vendor,
    # often per-site conversation - the chart filters on both, summing rows
    # client-side. Quarter buckets sort lexically within their band, so
    # (band, bucket) is a complete ordering and first_lapse is not needed.
    runway = (await session.execute(text("""
        SELECT CASE
                 WHEN d.warranty_expires < CURRENT_DATE THEN 'Expired'
                 WHEN d.warranty_expires <= CURRENT_DATE + 730
                   THEN to_char(d.warranty_expires, 'YYYY "Q"Q')
                 ELSE 'Beyond 2 years'
               END AS bucket,
               CASE
                 WHEN d.warranty_expires < CURRENT_DATE THEN 0
                 WHEN d.warranty_expires <= CURRENT_DATE + 730 THEN 1
                 ELSE 2
               END AS band,
               COALESCE(v.name, 'Unrecorded') AS vendor,
               dc.code AS dc,
               count(*) AS n
        FROM device d
        LEFT JOIN vendor v ON v.id = d.vendor_id
        LEFT JOIN rack r ON r.id = d.rack_id
        LEFT JOIN rack_row rr ON rr.id = r.row_id
        LEFT JOIN room rm ON rm.id = COALESCE(rr.room_id, d.room_id)
        LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
        WHERE d.warranty_expires IS NOT NULL
          AND d.lifecycle NOT IN ('decommissioned', 'retired')
        GROUP BY 1, 2, v.name, dc.code
        ORDER BY band, bucket
    """))).mappings().all()

    # Cover as four states, on the same threshold and the same live-estate
    # scope the runway uses - so the two charts beside each other cannot
    # disagree about how many assets there are to cover. Dimensioned by the
    # same (vendor, site) pair as the runway, for the same filters.
    cover = (await session.execute(text("""
        SELECT CASE
                 WHEN d.warranty_expires IS NULL THEN 'unknown'
                 WHEN d.warranty_expires < CURRENT_DATE THEN 'expired'
                 WHEN d.warranty_expires
                   <= CURRENT_DATE + CAST(:expiring AS integer) THEN 'expiring'
                 ELSE 'active'
               END AS state,
               COALESCE(v.name, 'Unrecorded') AS vendor,
               dc.code AS dc,
               count(*) AS n
        FROM device d
        LEFT JOIN vendor v ON v.id = d.vendor_id
        LEFT JOIN rack r ON r.id = d.rack_id
        LEFT JOIN rack_row rr ON rr.id = r.row_id
        LEFT JOIN room rm ON rm.id = COALESCE(rr.room_id, d.room_id)
        LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
        WHERE d.lifecycle NOT IN ('decommissioned', 'retired')
        GROUP BY 1, v.name, dc.code
    """), {"expiring": EXPIRING_DAYS})).mappings().all()

    # How much of the estate carries each field. The point is not the fields
    # that are full - it is the ones that are not, because every empty column
    # here is a chart or a filter somebody expects to work and finds blank.
    # Grouped by (site, type) so the chart can filter to "which fields are
    # empty on the PDUs in DC1" - the ratios stay honest under any filter
    # because every row carries its own denominator.
    completeness = (await session.execute(text("""
        SELECT dc.code AS dc,
               COALESCE(dt.display_name, d.device_type) AS type_label,
               count(*)                                              AS total,
               count(d.serial_number)                                AS serial_number,
               count(d.asset_tag)                                    AS asset_tag,
               count(d.owner_group)                                  AS owner_group,
               count(d.cost_centre)                                  AS cost_centre,
               count(d.warranty_expires)                             AS warranty_expires,
               count(d.purchase_date)                                AS purchase_date,
               count(d.install_date)                                 AS install_date,
               count(d.supplier_id)                                  AS supplier_id,
               count(*) FILTER (WHERE d.rack_id IS NOT NULL)         AS placement
        FROM device d
        LEFT JOIN device_type dt ON dt.code = d.device_type
        LEFT JOIN rack r ON r.id = d.rack_id
        LEFT JOIN rack_row rr ON rr.id = r.row_id
        LEFT JOIN room rm ON rm.id = COALESCE(rr.room_id, d.room_id)
        LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
        WHERE d.lifecycle NOT IN ('decommissioned', 'retired')
        GROUP BY dc.code, 2
    """))).mappings().all()

    # dc and room ship as their own fields, not only fused into the label: the
    # chart filters on them, and parsing them back out of the label would tie
    # the filter to a display convention. type_label is the cross dimension -
    # "where do the PDUs sit" - summed away client-side when unfiltered.
    by_room = (await session.execute(text("""
        SELECT dc.code AS dc, rm.name AS room,
               COALESCE(dt.display_name, d.device_type) AS type_label,
               dc.code || ' · ' || rm.name AS label, count(d.id) AS n
        FROM device d
        LEFT JOIN device_type dt ON dt.code = d.device_type
        LEFT JOIN rack r ON r.id = d.rack_id
        LEFT JOIN rack_row rr ON rr.id = r.row_id
        JOIN room rm ON rm.id = COALESCE(rr.room_id, d.room_id)
        JOIN datacenter dc ON dc.id = rm.datacenter_id
        WHERE d.lifecycle NOT IN ('decommissioned', 'retired')
        GROUP BY dc.code, rm.name, 3 ORDER BY n DESC
    """))).mappings().all()

    # Where things physically are. "Not placed" is a genuine third state and
    # not the same as floor-standing: a chiller in a plant room is placed, it
    # simply has no rack. Calling it unplaced would report the estate's own
    # design as a data gap.
    # Dimensioned by (type, site). A device that is not placed has no site,
    # so it shows under "All sites" only - which is where somebody hunting
    # for unplaced kit starts anyway.
    placement = (await session.execute(text("""
        SELECT CASE WHEN d.rack_id IS NOT NULL THEN 'In a rack'
                    WHEN d.room_id IS NOT NULL THEN 'Floor-standing'
                    ELSE 'Not placed' END AS label,
               COALESCE(dt.display_name, d.device_type) AS type_label,
               dc.code AS dc,
               count(*) AS n
        FROM device d
        LEFT JOIN device_type dt ON dt.code = d.device_type
        LEFT JOIN rack r ON r.id = d.rack_id
        LEFT JOIN rack_row rr ON rr.id = r.row_id
        LEFT JOIN room rm ON rm.id = COALESCE(rr.room_id, d.room_id)
        LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
        WHERE d.lifecycle NOT IN ('decommissioned', 'retired')
        GROUP BY 1, 2, dc.code
    """))).mappings().all()

    # Spend rows carry the contract's status on the same expiring threshold
    # the cover charts use: "what am I paying now" and "what lapsed" are
    # different meetings, and a single all-time sum answers neither.
    spend = (await session.execute(text("""
        SELECT COALESCE(s.name, 'No supplier recorded') AS label,
               CASE
                 WHEN c.end_date < CURRENT_DATE THEN 'expired'
                 WHEN c.end_date <= CURRENT_DATE + CAST(:expiring AS integer)
                   THEN 'expiring'
                 ELSE 'active'
               END AS status,
               count(*) AS contracts,
               COALESCE(sum(c.cost), 0)::float AS total
        FROM support_contract c
        LEFT JOIN supplier s ON s.id = c.supplier_id
        GROUP BY 1, 2 ORDER BY total DESC
    """), {"expiring": EXPIRING_DAYS})).mappings().all()

    # What still FITS, which is the number fragmentation actually costs. Total
    # free U says nothing about placeability: 1392U spread as 1U slivers takes
    # hundreds of pizza boxes and not one blade chassis. Computed from
    # CONTIGUOUS gaps - a gaps-and-islands walk over every rack unit, cheap at
    # 1,848 units - and each gap of length L fits floor(L / size) devices of
    # that size.
    #
    # Planned placeholders count as occupied, because they are: a reservation's
    # units are spoken for and must not be offered as placeable.
    #
    # Sizes are the chassis heights that exist: 1U-4U servers, 6U/8U for blade
    # chassis. A continuous axis would imply 5U equipment somebody could buy.
    # Dimensioned by (site, room): "can Hall A take another blade chassis"
    # is the install question. Fits are additive per rack - each contributes
    # floor(gap / size) - so the filtered figure is an exact client-side sum.
    fragmentation = (await session.execute(text("""
        WITH units AS (
            SELECT r.id AS rack_id, gs.u
            FROM rack r, generate_series(1, r.u_height) AS gs(u)
        ), occupied AS (
            SELECT d.rack_id, gs.u
            FROM device d,
                 generate_series(d.u_start, d.u_start + d.u_height - 1) AS gs(u)
            WHERE d.rack_id IS NOT NULL AND d.u_start IS NOT NULL
        ), free_units AS (
            SELECT un.rack_id, un.u,
                   un.u - row_number() OVER (PARTITION BY un.rack_id
                                             ORDER BY un.u) AS grp
            FROM units un
            LEFT JOIN occupied o ON o.rack_id = un.rack_id AND o.u = un.u
            WHERE o.u IS NULL
        ), gaps AS (
            SELECT rack_id, count(*) AS len
            FROM free_units GROUP BY rack_id, grp
        ), located AS (
            SELECT r.id AS rack_id, rm.name AS room, dc.code AS dc
            FROM rack r
            LEFT JOIN rack_row rr ON rr.id = r.row_id
            LEFT JOIN room rm ON rm.id = rr.room_id
            LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
        )
        SELECT l.dc, l.room, s.size,
               COALESCE(sum(g.len / s.size), 0)::int AS fits
        FROM located l
        CROSS JOIN (VALUES (1),(2),(3),(4),(6),(8)) AS s(size)
        LEFT JOIN gaps g ON g.rack_id = l.rack_id AND g.len >= s.size
        GROUP BY l.dc, l.room, s.size ORDER BY s.size
    """))).mappings().all()

    return {
        "composition": [dict(r) for r in composition],
        "by_lifecycle": [dict(r) for r in by_lifecycle],
        "rack_space": [dict(r) for r in space],
        "rack_fill": [dict(r) for r in fill],
        "floor_space": [dict(r) for r in floor],
        "cover_state": [dict(r) for r in cover],
        "warranty_runway": [dict(r) for r in runway],
        "by_room": [dict(r) for r in by_room],
        "placement": [dict(r) for r in placement],
        "contract_spend": [dict(r) for r in spend],
        "fragmentation": [dict(r) for r in fragmentation],
        "completeness": [dict(r) for r in completeness],
    }
