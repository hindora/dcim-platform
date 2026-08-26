"""Bulk per-room and per-site roll-ups for the thermal, power and utilisation pages.

These pages each show every room in the estate at once. That rules out calling
the existing per-scope services in a loop: `capacity.report` alone issues five
queries, so fifteen rooms would be seventy-five round trips to render one table
that has to stay open on a wall display.

So each page gets ONE query that groups by room and carries the datacentre id
with it. The site-level rollup is then folded in Python from the same rows -
weighted by sample count, never by averaging averages, which is how a room with
four sensors ends up outvoting one with four hundred.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_taxonomy import ALARM, ALERT, DETECTIONS

# Device -> (datacenter, room). Same resolution as the home page: a device may
# be racked, or stand on the floor in a room with no rack.
_DEV_CTE = """
    dev AS (
        SELECT d.id                                          AS device_id,
               COALESCE(rm.datacenter_id, rm2.datacenter_id) AS datacenter_id,
               COALESCE(rr.room_id, d.room_id)               AS room_id
        FROM device d
        LEFT JOIN rack     r   ON r.id  = d.rack_id
        LEFT JOIN rack_row rr  ON rr.id = r.row_id
        LEFT JOIN room     rm  ON rm.id = rr.room_id
        LEFT JOIN room     rm2 ON rm2.id = d.room_id
        WHERE d.lifecycle <> 'decommissioned'
    )
"""

# Every room, whether or not it reports anything. A room that has gone dark is
# the row an operator most needs to see, and an inner join would delete it.
_ROOMS = """
    SELECT rm.id            AS room_id,
           rm.name          AS room_name,
           rm.floor         AS floor,
           rm.room_type     AS room_type,
           rm.room_class    AS room_class,
           rm.datacenter_id AS datacenter_id,
           dc.code          AS site_code,
           dc.name          AS site_name
    FROM room rm
    JOIN datacenter dc ON dc.id = rm.datacenter_id
"""


async def thermal(session: AsyncSession, *, focus_start: datetime,
                  focus_end: datetime, compare_start: datetime,
                  compare_end: datetime, low_c: float,
                  high_c: float) -> list[dict[str, Any]]:
    """Rack intake temperature per room, over two windows.

    Sums and counts rather than averages, so the caller can roll rooms up into
    a site without averaging averages. `in_band` counts samples inside the
    recommended envelope the caller passes in - compliance is a share of
    READINGS, not of racks, because one rack sampled every ten seconds and one
    sampled hourly do not deserve equal weight.

    Both windows are read in a single pass. Two queries would double the scan
    of the same hypertable chunks for no benefit.
    """
    rows = (await session.execute(text(f"""
        WITH {_DEV_CTE},
        rooms AS ({_ROOMS}),
        s AS (
            SELECT dev.room_id, t.ts, t.value
            FROM telemetry_sample t
            JOIN metric m ON m.id = t.metric_id
            JOIN dev      ON dev.device_id = t.device_id
            WHERE m.key = 'inlet_temperature'
              AND ((t.ts >= :f0 AND t.ts < :f1)
                OR (t.ts >= :c0 AND t.ts < :c1))
        ),
        agg AS (
            SELECT room_id,
                   sum(value)   FILTER (WHERE ts >= :f0 AND ts < :f1) AS f_sum,
                   count(*)     FILTER (WHERE ts >= :f0 AND ts < :f1) AS f_n,
                   max(value)   FILTER (WHERE ts >= :f0 AND ts < :f1) AS f_max,
                   count(*)     FILTER (WHERE ts >= :f0 AND ts < :f1
                                          AND value >= :low AND value <= :high)
                                                                      AS f_in_band,
                   sum(value)   FILTER (WHERE ts >= :c0 AND ts < :c1) AS c_sum,
                   count(*)     FILTER (WHERE ts >= :c0 AND ts < :c1) AS c_n,
                   max(value)   FILTER (WHERE ts >= :c0 AND ts < :c1) AS c_max
            FROM s GROUP BY room_id
        ),
        racks AS (
            SELECT rr.room_id, count(*) AS rack_count
            FROM rack r JOIN rack_row rr ON rr.id = r.row_id
            GROUP BY rr.room_id
        )
        SELECT rooms.room_id::text       AS room_id,
               rooms.room_name           AS room_name,
               rooms.floor               AS floor,
               rooms.room_type           AS room_type,
               rooms.room_class          AS room_class,
               rooms.datacenter_id::text AS datacenter_id,
               rooms.site_code           AS site_code,
               rooms.site_name           AS site_name,
               COALESCE(racks.rack_count, 0) AS rack_count,
               agg.f_sum, agg.f_n, agg.f_max, agg.f_in_band,
               agg.c_sum, agg.c_n, agg.c_max
        FROM rooms
        LEFT JOIN agg   ON agg.room_id   = rooms.room_id
        LEFT JOIN racks ON racks.room_id = rooms.room_id
        ORDER BY rooms.site_code, rooms.room_name
    """), {"f0": focus_start, "f1": focus_end,
           "c0": compare_start, "c1": compare_end,
           "low": low_c, "high": high_c})).mappings().all()
    return [dict(r) for r in rows]


async def power_window(session: AsyncSession, *, start: datetime, end: datetime,
                       compare_start: datetime, compare_end: datetime,
                       bucket: timedelta) -> list[dict[str, Any]]:
    """Room power over a window, bucketed, split into IT / cooling / other.

    The peak is COINCIDENT: loads are summed inside each bucket first and the
    maximum taken across buckets. Taking each device's own peak and adding them
    would report a total that never happened, and sizing a feed off it is how
    you buy a breaker nobody needed.

    The `power` category is excluded from every total. A PDU meters the servers
    plugged into it, so adding both counts the same watts twice - the same rule
    the site KPI drawer follows.
    """
    rows = (await session.execute(text(f"""
        WITH {_DEV_CTE},
        rooms AS ({_ROOMS}),
        b AS (
            SELECT time_bucket(:bucket, t.ts) AS bkt,
                   dev.room_id                                  AS room_id,
                   dt.category                                  AS cat,
                   t.device_id                                  AS device_id,
                   (t.ts >= :s AND t.ts < :e)                   AS is_focus,
                   avg(t.value)                                 AS w
            FROM telemetry_sample t
            JOIN metric m       ON m.id = t.metric_id
            JOIN dev            ON dev.device_id = t.device_id
            JOIN device d       ON d.id = t.device_id
            JOIN device_type dt ON dt.code = d.device_type
            WHERE m.key = 'power_draw'
              AND t.instance = ''
              AND ((t.ts >= :s AND t.ts < :e) OR (t.ts >= :c0 AND t.ts < :c1))
            GROUP BY 1, 2, 3, 4, 5
        ),
        per_bucket AS (
            SELECT bkt, room_id, is_focus,
                   COALESCE(sum(w) FILTER (WHERE cat IN ('it', 'network')), 0)/1000.0
                       AS it_kw,
                   COALESCE(sum(w) FILTER (WHERE cat = 'cooling'), 0)/1000.0
                       AS cooling_kw,
                   COALESCE(sum(w) FILTER (
                       WHERE cat NOT IN ('it', 'network', 'cooling', 'power')), 0)/1000.0
                       AS other_kw
            FROM b GROUP BY 1, 2, 3
        ),
        agg AS (
            SELECT room_id,
                   avg(it_kw)      FILTER (WHERE is_focus) AS avg_it,
                   max(it_kw)      FILTER (WHERE is_focus) AS peak_it,
                   avg(cooling_kw) FILTER (WHERE is_focus) AS avg_cooling,
                   max(cooling_kw) FILTER (WHERE is_focus) AS peak_cooling,
                   avg(other_kw)   FILTER (WHERE is_focus) AS avg_other,
                   max(other_kw)   FILTER (WHERE is_focus) AS peak_other,
                   avg(it_kw + cooling_kw + other_kw) FILTER (WHERE is_focus)
                       AS avg_total,
                   max(it_kw + cooling_kw + other_kw) FILTER (WHERE is_focus)
                       AS peak_total,
                   count(*) FILTER (WHERE is_focus)       AS buckets,
                   avg(it_kw + cooling_kw + other_kw) FILTER (WHERE NOT is_focus)
                       AS prev_total,
                   count(*) FILTER (WHERE NOT is_focus)   AS prev_buckets
            FROM per_bucket GROUP BY room_id
        )
        SELECT rooms.room_id::text       AS room_id,
               rooms.room_name           AS room_name,
               rooms.floor               AS floor,
               rooms.room_class          AS room_class,
               rooms.datacenter_id::text AS datacenter_id,
               rooms.site_code           AS site_code,
               rooms.site_name           AS site_name,
               agg.avg_it, agg.peak_it, agg.avg_cooling, agg.peak_cooling,
               agg.avg_other, agg.peak_other, agg.avg_total, agg.peak_total,
               agg.buckets, agg.prev_total, agg.prev_buckets
        FROM rooms
        LEFT JOIN agg ON agg.room_id = rooms.room_id
        ORDER BY rooms.site_code, rooms.room_name
    """), {"s": start, "e": end, "c0": compare_start, "c1": compare_end,
           "bucket": bucket})).mappings().all()
    return [dict(r) for r in rows]


async def power_live(session: AsyncSession) -> list[dict[str, Any]]:
    """Instantaneous room power from the hot mirror.

    `device_state.power_w` is the newest reading the ingest worker wrote, so
    this answers "right now" without touching the hypertable at all.
    """
    rows = (await session.execute(text(f"""
        WITH {_DEV_CTE},
        rooms AS ({_ROOMS}),
        agg AS (
            SELECT dev.room_id,
                   COALESCE(sum(ds.power_w) FILTER (
                       WHERE dt.category IN ('it', 'network')), 0)/1000.0 AS avg_it,
                   COALESCE(sum(ds.power_w) FILTER (
                       WHERE dt.category = 'cooling'), 0)/1000.0          AS avg_cooling,
                   COALESCE(sum(ds.power_w) FILTER (
                       WHERE dt.category NOT IN
                             ('it', 'network', 'cooling', 'power')), 0)/1000.0
                                                                          AS avg_other,
                   count(*) FILTER (WHERE ds.power_w IS NOT NULL)         AS reporting
            FROM dev
            JOIN device_state ds ON ds.device_id = dev.device_id
            JOIN device d        ON d.id = dev.device_id
            JOIN device_type dt  ON dt.code = d.device_type
            GROUP BY dev.room_id
        )
        SELECT rooms.room_id::text       AS room_id,
               rooms.room_name           AS room_name,
               rooms.floor               AS floor,
               rooms.room_class          AS room_class,
               rooms.datacenter_id::text AS datacenter_id,
               rooms.site_code           AS site_code,
               rooms.site_name           AS site_name,
               agg.avg_it, agg.avg_cooling, agg.avg_other, agg.reporting
        FROM rooms
        LEFT JOIN agg ON agg.room_id = rooms.room_id
        ORDER BY rooms.site_code, rooms.room_name
    """))).mappings().all()
    return [dict(r) for r in rows]


async def utilisation(session: AsyncSession) -> list[dict[str, Any]]:
    """Space, power and cooling headroom per room.

    Three denominators, three different levels of confidence, so each is
    returned with the raw parts rather than a finished percentage:

    * space    - rack U installed against rack U occupied. Inventory, exact.
    * power    - the room's own `design_it_kw` if it has one, else the summed
                 nameplate of the PDUs and RPPs standing in it. The fallback is
                 INSTALLED capacity, not usable capacity: on a 2N floor half of
                 it exists to be idle, so the service labels which one it used.
    * cooling  - installed cooling nameplate from `cooling_capacity`, against
                 the IT heat in the room.
    """
    rows = (await session.execute(text(f"""
        WITH {_DEV_CTE},
        rooms AS ({_ROOMS}),
        space AS (
            SELECT rr.room_id,
                   count(*)                      AS rack_count,
                   COALESCE(sum(r.u_height), 0)  AS total_u
            FROM rack r JOIN rack_row rr ON rr.id = r.row_id
            GROUP BY rr.room_id
        ),
        used AS (
            SELECT rr.room_id, COALESCE(sum(d.u_height), 0) AS used_u
            FROM device d
            JOIN rack r      ON r.id = d.rack_id
            JOIN rack_row rr ON rr.id = r.row_id
            WHERE d.lifecycle <> 'decommissioned' AND d.u_start IS NOT NULL
            GROUP BY rr.room_id
        ),
        load AS (
            SELECT dev.room_id,
                   COALESCE(sum(ds.power_w) FILTER (
                       WHERE dt.category IN ('it', 'network')), 0)/1000.0 AS it_kw,
                   COALESCE(sum(ds.power_w) FILTER (
                       WHERE dt.category = 'cooling'), 0)/1000.0          AS cooling_kw
            FROM dev
            JOIN device_state ds ON ds.device_id = dev.device_id
            JOIN device d        ON d.id = dev.device_id
            JOIN device_type dt  ON dt.code = d.device_type
            GROUP BY dev.room_id
        ),
        supply AS (
            SELECT dev.room_id,
                   COALESCE(sum(m.rated_power_w), 0)/1000.0 AS rated_kw,
                   count(*)                                 AS units
            FROM dev
            JOIN device d       ON d.id = dev.device_id
            JOIN model m        ON m.id = d.model_id
            WHERE d.device_type IN ('pdu', 'rpp')
              AND m.rated_power_w IS NOT NULL AND m.rated_power_w > 0
            GROUP BY dev.room_id
        ),
        cooling AS (
            SELECT dev.room_id,
                   sum(v.kw) AS capacity_kw,
                   count(*)  AS units
            FROM dev
            JOIN device d ON d.id = dev.device_id
            JOIN LATERAL (
                SELECT t.value/1000.0 AS kw
                FROM telemetry_sample t
                JOIN metric m ON m.id = t.metric_id
                WHERE t.device_id = d.id AND m.key = 'cooling_capacity'
                  AND t.ts > now() - interval '24 hours'
                ORDER BY t.ts DESC LIMIT 1
            ) v ON TRUE
            GROUP BY dev.room_id
        )
        SELECT rooms.room_id::text       AS room_id,
               rooms.room_name           AS room_name,
               rooms.floor               AS floor,
               rooms.room_class          AS room_class,
               rooms.datacenter_id::text AS datacenter_id,
               rooms.site_code           AS site_code,
               rooms.site_name           AS site_name,
               rm.design_it_kw           AS design_it_kw,
               rm.designed_racks         AS designed_racks,
               rm.width_m                AS width_m,
               rm.depth_m                AS depth_m,
               COALESCE(space.rack_count, 0) AS rack_count,
               COALESCE(space.total_u, 0)    AS total_u,
               COALESCE(used.used_u, 0)      AS used_u,
               COALESCE(load.it_kw, 0)       AS it_kw,
               COALESCE(load.cooling_kw, 0)  AS cooling_kw,
               supply.rated_kw               AS supply_rated_kw,
               supply.units                  AS supply_units,
               cooling.capacity_kw           AS cooling_capacity_kw,
               cooling.units                 AS cooling_units
        FROM rooms
        JOIN room rm      ON rm.id = rooms.room_id
        LEFT JOIN space   ON space.room_id   = rooms.room_id
        LEFT JOIN used    ON used.room_id    = rooms.room_id
        LEFT JOIN load    ON load.room_id    = rooms.room_id
        LEFT JOIN supply  ON supply.room_id  = rooms.room_id
        LEFT JOIN cooling ON cooling.room_id = rooms.room_id
        ORDER BY rooms.site_code, rooms.room_name
    """))).mappings().all()
    return [dict(r) for r in rows]


async def site_design(session: AsyncSession) -> dict[str, Any]:
    """`design_it_kw` per site, for the site rows of the utilisation page."""
    rows = (await session.execute(text("""
        SELECT id::text AS id, design_it_kw FROM datacenter
    """))).mappings().all()
    return {r["id"]: r["design_it_kw"] for r in rows}


async def alarms_by_room(session: AsyncSession, *,
                         categories: list[str]) -> list[dict[str, Any]]:
    """Open root alarms of one category, grouped by room - with the alerts beside them.

    The drill-down behind a counter. `qty` is alarms only, exactly the
    population the counter totals: a drill-down that disagrees with the number
    that opened it is worse than no drill-down. Every other alarm figure here -
    devices, severities, the facets - is alarms only for the same reason.
    `devices_all` is the same count over both classes, for the panels that show
    both: there, the alarm-only figure would sit next to an alert column it
    does not describe.

    `alerts` is the informational count for the same room and category. A room
    with two cooling alarms and forty cooling alerts is a different room from
    one with two and none, and an engineer deciding where to walk first wants
    to know which they are looking at.

    Both classes decide the row set, because the category counter that opens
    this panel counts both: a panel listing fewer rooms than the tile that
    opened it is the disagreement this whole area exists to avoid. The columns
    keep them apart, and every alarm-side aggregate below keeps its filter.

    Several categories in one call, because a grouped counter - Cooling is
    cooling plus environmental - is one question. Asking twice and adding the
    answers in the browser produced two rows for one room, and no honest way to
    count its devices: a device with a fault in both domains is one device, and
    only the database can say so. `count(DISTINCT)` over the union does.
    """
    # Every alarm-side aggregate carries the same filter: these describe the
    # alarms the counter opened, not the alerts sitting beside them.
    sev_cols = ",\n               ".join(
        f"count(*) FILTER (WHERE response_class = '{ALARM}' "
        f"AND severity = '{s}') AS {s.lower()}"
        for s in ("CRITICAL", "MAJOR", "MINOR", "WARNING")
    )
    facet_cols = ",\n               ".join(
        f"count(*) FILTER (WHERE response_class = '{ALARM}' "
        f"AND detection = '{d}') AS detected_{d}"
        for d in DETECTIONS
    )

    rows = (await session.execute(text(f"""
        WITH {_DEV_CTE},
        cat AS (
            SELECT dev.datacenter_id, dev.room_id, a.severity::text AS severity,
                   a.device_id, a.detection AS detection,
                   a.category AS category, a.response_class AS response_class
            FROM alarm a
            JOIN dev ON dev.device_id = a.device_id
            WHERE a.state <> 'CLEARED' AND a.is_symptom = false
        )
        SELECT rm.id::text            AS room_id,
               rm.name                AS room_name,
               rm.floor               AS floor,
               dc.id::text            AS datacenter_id,
               dc.code                AS site_code,
               dc.name                AS site_name,
               count(*) FILTER (WHERE response_class = '{ALARM}')  AS qty,
               count(*) FILTER (WHERE response_class = '{ALERT}')  AS alerts,
               count(DISTINCT cat.device_id)
                   FILTER (WHERE response_class = '{ALARM}')       AS devices,
               -- Both classes. A panel showing alarms AND alerts printing a
               -- device count that only saw the alarms reads as zero devices
               -- beside four alerts, which looks like a broken column rather
               -- than a deliberate one. The caller picks the figure that
               -- matches the columns it is showing.
               count(DISTINCT cat.device_id)                       AS devices_all,
               {sev_cols},
               {facet_cols}
        FROM cat
        JOIN room rm       ON rm.id = cat.room_id
        JOIN datacenter dc ON dc.id = cat.datacenter_id
        WHERE cat.category = ANY(:categories)
        GROUP BY rm.id, rm.name, rm.floor, dc.id, dc.code, dc.name
        -- Every room with anything open in this category, because that is
        -- what the counter that opens this panel now counts. The two columns
        -- keep the classes apart on the row; the row set no longer does.
        HAVING count(*) > 0
        ORDER BY qty DESC, dc.code, rm.name
    """), {"categories": categories})).mappings().all()
    return [dict(r) for r in rows]


async def unlocated_alarms_by_category(session: AsyncSession, *,
                                       categories: list[str]) -> dict[str, int]:
    """Alarms of a category that resolve to no room.

    Platform conditions hang off devices with no location. They are counted in
    the strip, so the drill-down has to account for them or the modal will
    appear to have lost rows. Both classes, matching the counter.
    """
    row = (await session.execute(text(f"""
        WITH {_DEV_CTE},
        cat AS (
            SELECT dev.room_id, a.category AS category,
                   a.response_class AS response_class
            FROM alarm a
            LEFT JOIN dev ON dev.device_id = a.device_id
            WHERE a.state <> 'CLEARED' AND a.is_symptom = false
        )
        SELECT count(*)                                       AS n,
               count(*) FILTER (WHERE response_class = '{ALARM}') AS alarms
        FROM cat
        WHERE category = ANY(:categories) AND room_id IS NULL
    """), {"categories": categories})).mappings().first()
    if row is None:
        return {"total": 0, "alarms": 0}
    return {"total": int(row["n"]), "alarms": int(row["alarms"])}


async def room(session: AsyncSession, room_id: str) -> dict[str, Any] | None:
    """Identity for one room, plus its site."""
    row = (await session.execute(text("""
        SELECT rm.id::text AS id, rm.name, rm.room_type, rm.room_class, rm.floor,
               rm.design_it_kw, rm.designed_racks,
               dc.id::text AS datacenter_id, dc.code AS site_code,
               dc.name AS site_name, dc.city, dc.country
        FROM room rm
        JOIN datacenter dc ON dc.id = rm.datacenter_id
        WHERE rm.id = CAST(:id AS uuid)
    """), {"id": room_id})).mappings().first()
    return dict(row) if row else None


async def room_census(session: AsyncSession, room_id: str) -> dict[str, Any]:
    """What is in the room and how much of it is answering."""
    row = (await session.execute(text(f"""
        WITH {_DEV_CTE}
        SELECT count(*)                                            AS devices,
               count(*) FILTER (WHERE ds.status = 'ONLINE')        AS online,
               count(*) FILTER (WHERE ds.status = 'OFFLINE')       AS offline,
               count(*) FILTER (WHERE dt.category = 'cooling')     AS cooling_units,
               count(*) FILTER (WHERE dt.category = 'cooling'
                                  AND ds.status = 'ONLINE')        AS cooling_online,
               count(*) FILTER (WHERE dt.category = 'power')       AS power_units,
               count(*) FILTER (WHERE dt.category = 'power'
                                  AND ds.status = 'ONLINE')        AS power_online
        FROM dev
        JOIN device d       ON d.id = dev.device_id
        JOIN device_type dt ON dt.code = d.device_type
        LEFT JOIN device_state ds ON ds.device_id = dev.device_id
        WHERE dev.room_id = CAST(:id AS uuid)
    """), {"id": room_id})).mappings().first()
    return dict(row) if row else {}


async def room_updated(session: AsyncSession, room_id: str) -> datetime | None:
    """Newest telemetry timestamp anywhere in the room.

    The room-level answer to "is this data worth reading". Bounded to a day so
    the query stays on recent chunks; a room silent for longer reports None,
    which the UI renders as unknown rather than as a stale date.
    """
    return (await session.execute(text(f"""
        WITH {_DEV_CTE}
        SELECT max(t.ts) FROM telemetry_sample t
        JOIN dev ON dev.device_id = t.device_id
        WHERE dev.room_id = CAST(:id AS uuid)
          AND t.ts > now() - interval '24 hours'
    """), {"id": room_id})).scalar()
