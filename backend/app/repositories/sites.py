"""Per-site and per-room roll-ups for the home page.

This page is an ALARM console. Every count here is `response_class = 'alarm'`:
conditions that require a response now. Alerts - wear, hygiene, stale telemetry,
everything informational - are classified, stored and queryable, and they are
not on this screen. A console that lists four hundred things nobody will act on
tonight is a console operators stop reading.

Reach the informational ones with `/alarms?response_class=alert`.


The home page is a table of sites with an alert indicator per category, and it
must not fan out. One query per tab, both driven by the same device-to-location
CTE the dashboard uses, so a device that hangs off a room directly rather than
off a rack still lands in the right site.

Counts are ROOTS ONLY (`is_symptom = false`). An OOB switch failure that lights
up twenty-one downstream devices is one alert on this page, not twenty-two.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_taxonomy import ALARM, CATEGORIES, DETECTIONS

# Device -> (datacenter, room). A device may be racked, or sit in a room with no
# rack (facility gear, floor-standing plant), so both paths are resolved and
# coalesced. Decommissioned devices are excluded everywhere.
_DEV_CTE = """
    dev AS (
        SELECT d.id                                          AS device_id,
               COALESCE(rm.datacenter_id, rm2.datacenter_id) AS datacenter_id,
               COALESCE(rr.room_id, d.room_id)               AS room_id,
               d.device_type                                 AS device_type
        FROM device d
        LEFT JOIN rack     r   ON r.id  = d.rack_id
        LEFT JOIN rack_row rr  ON rr.id = r.row_id
        LEFT JOIN room     rm  ON rm.id = rr.room_id
        LEFT JOIN room     rm2 ON rm2.id = d.room_id
        WHERE d.lifecycle <> 'decommissioned'
    )
"""

# Open root alarms, already bucketed. State is ACTIVE or ACKNOWLEDGED: an
# acknowledged alarm is still a live condition, and hiding it here is how a
# known fault becomes a forgotten one.
#
# `response_class = 'alarm'` is the other filter that matters: this CTE is the
# whole page's population, so the eight category counters, the severity columns
# and the drill-downs all describe faults and nothing else.
_ALARM_CTE = f"""
    alarm_cat AS (
        SELECT dev.datacenter_id,
               dev.room_id,
               a.severity::text AS severity,
               a.category       AS category,
               a.detection      AS detection
        FROM alarm a
        JOIN dev ON dev.device_id = a.device_id
        WHERE a.state <> 'CLEARED' AND a.is_symptom = false
          AND a.response_class = '{ALARM}'
    )
"""

# The category is READ here, not recomputed. Phase 1 stamps it at raise time
# through the role-sensitive classifier, so deriving it in the roll-up would
# mean joining every alarm through device to device_type on every count - and
# would rewrite history the moment a device is re-typed.
_CATEGORY_COLUMNS = ",\n".join(
    f"count(*) FILTER (WHERE category = '{c}') AS alerts_{c}" for c in CATEGORIES
)

# HOW each one was found, across all eight categories. This is what makes
# "only what analytics noticed" a filter rather than a category that grows
# every time a detector is added.
_DETECTION_COLUMNS = ",\n".join(
    f"count(*) FILTER (WHERE detection = '{d}') AS detected_{d}"
    for d in DETECTIONS
)

_ALL_CATEGORY_COLUMNS = ",\n".join(
    (_CATEGORY_COLUMNS, _DETECTION_COLUMNS))

_SEVERITY_COLUMNS = """
    count(*)                                                    AS alerts_total,
    count(*) FILTER (WHERE severity = 'CRITICAL')               AS crit,
    count(*) FILTER (WHERE severity = 'MAJOR')                  AS major,
    count(*) FILTER (WHERE severity IN ('MINOR', 'WARNING'))    AS minor
"""

# A site or room with no open alarm has no `agg` row at all, so every count
# needs its COALESCE. Generated from the same tuples as the counting columns:
# a category added to the taxonomy and forgotten here would read as absent
# rather than as an error.
_AGG_COALESCE = ",\n               ".join(
    f"COALESCE(agg.{name}, 0) AS {name}"
    for name in ([f"alerts_{c}" for c in CATEGORIES]
                 + [f"detected_{d}" for d in DETECTIONS])
)


async def site_rollups(session: AsyncSession) -> list[dict[str, Any]]:
    """One row per datacenter: identity, counts, and alerts by category."""
    rows = (await session.execute(text(f"""
        WITH {_DEV_CTE},
        {_ALARM_CTE},
        agg AS (
            SELECT datacenter_id,
                   {_ALL_CATEGORY_COLUMNS},
                   {_SEVERITY_COLUMNS}
            FROM alarm_cat GROUP BY datacenter_id
        ),
        devices AS (
            SELECT dev.datacenter_id,
                   count(*)                                          AS device_count,
                   count(*) FILTER (WHERE ds.status = 'ONLINE')      AS online_count,
                   count(*) FILTER (WHERE ds.status = 'OFFLINE')     AS offline_count
            FROM dev
            LEFT JOIN device_state ds ON ds.device_id = dev.device_id
            GROUP BY dev.datacenter_id
        ),
        rooms AS (
            SELECT datacenter_id, count(*) AS room_count
            FROM room GROUP BY datacenter_id
        )
        SELECT dc.id::text          AS id,
               dc.code              AS code,
               dc.name              AS name,
               dc.city              AS city,
               dc.country           AS country,
               dc.timezone          AS timezone,
               dc.design_it_kw      AS design_it_kw,
               COALESCE(rooms.room_count, 0)      AS room_count,
               COALESCE(devices.device_count, 0)  AS device_count,
               COALESCE(devices.online_count, 0)  AS online_count,
               COALESCE(devices.offline_count, 0) AS offline_count,
               COALESCE(agg.alerts_total, 0)      AS alerts_total,
               COALESCE(agg.crit, 0)              AS crit,
               COALESCE(agg.major, 0)             AS major,
               COALESCE(agg.minor, 0)             AS minor,
               {_AGG_COALESCE}
        FROM datacenter dc
        LEFT JOIN agg     ON agg.datacenter_id = dc.id
        LEFT JOIN devices ON devices.datacenter_id = dc.id
        LEFT JOIN rooms   ON rooms.datacenter_id = dc.id
        ORDER BY dc.code
    """))).mappings().all()
    return [dict(r) for r in rows]


async def room_rollups(session: AsyncSession,
                       datacenter_id: str | None = None) -> list[dict[str, Any]]:
    """One row per room, with the same shape as a site row.

    The home page renders these both as the ROOMS tab and as the expanded
    children of a site, so they deliberately share a schema.
    """
    where = "WHERE rm.datacenter_id = CAST(:dc AS uuid)" if datacenter_id else ""
    params = {"dc": datacenter_id} if datacenter_id else {}

    rows = (await session.execute(text(f"""
        WITH {_DEV_CTE},
        {_ALARM_CTE},
        agg AS (
            SELECT room_id,
                   {_ALL_CATEGORY_COLUMNS},
                   {_SEVERITY_COLUMNS}
            FROM alarm_cat GROUP BY room_id
        ),
        devices AS (
            SELECT dev.room_id,
                   count(*)                                     AS device_count,
                   count(*) FILTER (WHERE ds.status = 'OFFLINE') AS offline_count
            FROM dev
            LEFT JOIN device_state ds ON ds.device_id = dev.device_id
            GROUP BY dev.room_id
        ),
        racks AS (
            SELECT rr.room_id, count(*) AS rack_count
            FROM rack r JOIN rack_row rr ON rr.id = r.row_id
            GROUP BY rr.room_id
        )
        SELECT rm.id::text            AS id,
               rm.name                AS name,
               rm.room_type           AS room_type,
               rm.room_class          AS room_class,
               rm.floor               AS floor,
               rm.datacenter_id::text AS datacenter_id,
               dc.code                AS datacenter_code,
               COALESCE(racks.rack_count, 0)      AS rack_count,
               COALESCE(devices.device_count, 0)  AS device_count,
               COALESCE(devices.offline_count, 0) AS offline_count,
               COALESCE(agg.alerts_total, 0)      AS alerts_total,
               COALESCE(agg.crit, 0)              AS crit,
               COALESCE(agg.major, 0)             AS major,
               COALESCE(agg.minor, 0)             AS minor,
               {_AGG_COALESCE}
        FROM room rm
        JOIN datacenter dc ON dc.id = rm.datacenter_id
        LEFT JOIN agg     ON agg.room_id = rm.id
        LEFT JOIN devices ON devices.room_id = rm.id
        LEFT JOIN racks   ON racks.room_id = rm.id
        {where}
        ORDER BY dc.code, rm.name
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def fleet_alert_totals(session: AsyncSession) -> dict[str, Any]:
    """Fleet-wide counts for the alert strip across the top of the page.

    Deliberately does NOT join through to a location.

    Platform alarms - `ingest_stalled`, `collector_stale`, `ingest_lag_high` -
    hang off devices that resolve to no datacenter, so a location join silently
    drops them. Excluding them from a per-SITE row is correct, since a stalled
    ingest worker is not DC1's fault. Excluding them from the strip is not: it
    puts "Datapoint Alerts: 0" at the top of the screen at the exact moment the
    pipeline has stopped delivering datapoints, which is the one failure this
    page must never hide.
    """
    row = (await session.execute(text(f"""
        WITH alarm_cat AS (
            SELECT a.severity::text AS severity,
                   a.category       AS category,
                   a.detection      AS detection
            FROM alarm a
            WHERE a.state <> 'CLEARED' AND a.is_symptom = false
              AND a.response_class = '{ALARM}'
        )
        SELECT {_ALL_CATEGORY_COLUMNS},
               {_SEVERITY_COLUMNS}
        FROM alarm_cat
    """))).mappings().first()
    return dict(row) if row else {}


async def unlocated_alarms(session: AsyncSession) -> int:
    """Open root alarms that belong to no site.

    Surfaced so the difference between the strip total and the sum of the table
    rows is explained on the page rather than read as an arithmetic error.
    """
    row = (await session.execute(text(f"""
        WITH {_DEV_CTE}
        SELECT count(*) AS n
        FROM alarm a
        LEFT JOIN dev ON dev.device_id = a.device_id
        WHERE a.state <> 'CLEARED' AND a.is_symptom = false
          AND a.response_class = '{ALARM}'
          AND dev.datacenter_id IS NULL
    """))).mappings().first()
    return int(row["n"]) if row else 0


async def site_power(session: AsyncSession, datacenter_id: str) -> dict[str, Any]:
    """Instantaneous load for one site, split the way PUE needs it.

    IT is `it` + `network` only. Summing every device would double count - a
    PDU reports the draw of the servers plugged into it - so the power
    category is deliberately excluded from every total here.
    """
    row = (await session.execute(text(f"""
        WITH {_DEV_CTE}
        SELECT
          COALESCE(sum(ds.power_w) FILTER (WHERE dt.category IN ('it','network')), 0)/1000.0
              AS it_load_kw,
          COALESCE(sum(ds.power_w) FILTER (WHERE dt.category = 'cooling'), 0)/1000.0
              AS cooling_load_kw,
          COALESCE(sum(ds.power_w) FILTER (
              WHERE dt.category NOT IN ('it','network','cooling','power')), 0)/1000.0
              AS facility_other_kw,
          count(*) FILTER (WHERE ds.power_w IS NOT NULL) AS reporting_devices
        FROM dev
        JOIN device_state ds ON ds.device_id = dev.device_id
        JOIN device d        ON d.id = dev.device_id
        JOIN device_type dt  ON dt.code = d.device_type
        WHERE dev.datacenter_id = CAST(:dc AS uuid)
    """), {"dc": datacenter_id})).mappings().first()
    return dict(row) if row else {}


async def site_space(session: AsyncSession, datacenter_id: str) -> dict[str, Any]:
    """Rack U installed against rack U consumed.

    Zero-U devices (vertical PDUs, strapped-on probes) are excluded from the
    used total: they are real, but they occupy no U and counting them would
    make a rack look fuller than it is.
    """
    row = (await session.execute(text("""
        SELECT COALESCE(sum(r.u_height), 0) AS total_u,
               COALESCE((
                   SELECT sum(d.u_height)
                   FROM device d
                   JOIN rack r2      ON r2.id = d.rack_id
                   JOIN rack_row rr2 ON rr2.id = r2.row_id
                   JOIN room rm2     ON rm2.id = rr2.room_id
                   WHERE rm2.datacenter_id = CAST(:dc AS uuid)
                     AND d.lifecycle <> 'decommissioned'
                     AND d.u_start IS NOT NULL
               ), 0) AS used_u,
               count(*) AS rack_count
        FROM rack r
        JOIN rack_row rr ON rr.id = r.row_id
        JOIN room rm     ON rm.id = rr.room_id
        WHERE rm.datacenter_id = CAST(:dc AS uuid)
    """), {"dc": datacenter_id})).mappings().first()
    return dict(row) if row else {}


async def site_devices(session: AsyncSession, datacenter_id: str) -> dict[str, Any]:
    """Device census for one site."""
    row = (await session.execute(text(f"""
        WITH {_DEV_CTE}
        SELECT count(*)                                      AS total,
               count(*) FILTER (WHERE ds.status = 'ONLINE')  AS online,
               count(*) FILTER (WHERE ds.status = 'OFFLINE') AS offline,
               count(*) FILTER (WHERE ds.status = 'DEGRADED') AS degraded
        FROM dev
        LEFT JOIN device_state ds ON ds.device_id = dev.device_id
        WHERE dev.datacenter_id = CAST(:dc AS uuid)
    """), {"dc": datacenter_id})).mappings().first()
    return dict(row) if row else {}


async def site_endpoints(session: AsyncSession, datacenter_id: str) -> dict[str, Any]:
    """Endpoint census for the site - what is actually being polled."""
    row = (await session.execute(text(f"""
        WITH {_DEV_CTE}
        SELECT count(*)                                          AS total,
               count(*) FILTER (WHERE e.enabled)                 AS enabled,
               count(DISTINCT e.protocol)                        AS protocols
        FROM device_endpoint e
        JOIN dev ON dev.device_id = e.device_id
        WHERE dev.datacenter_id = CAST(:dc AS uuid)
    """), {"dc": datacenter_id})).mappings().first()
    return dict(row) if row else {}


async def site_alarms(session: AsyncSession, datacenter_id: str) -> dict[str, Any]:
    """Alert counts for one site, same buckets as the table."""
    row = (await session.execute(text(f"""
        WITH {_DEV_CTE},
        {_ALARM_CTE}
        SELECT {_ALL_CATEGORY_COLUMNS},
               {_SEVERITY_COLUMNS}
        FROM alarm_cat
        WHERE datacenter_id = CAST(:dc AS uuid)
    """), {"dc": datacenter_id})).mappings().first()
    return dict(row) if row else {}


async def site_weather(session: AsyncSession, datacenter_id: str) -> dict[str, Any]:
    """Outdoor air for one site, newest reading per metric.

    Read off the cooling-tower controllers, which is where a BMS keeps outdoor
    air: a tower is controlled to approach wet bulb, so the sensor is wired to
    it. Every tower at a site sees the same sky, so the newest sample across all
    of them is the site's weather - taking a max or an average across towers
    would only smear sensor noise.

    Sampled with a 2 h horizon rather than the usual hour: these points are
    slow-polled by design, and a tower staged off overnight is not a reason to
    report that the site has no weather.
    """
    rows = (await session.execute(text(f"""
        WITH {_DEV_CTE}
        SELECT DISTINCT ON (m.key)
               m.key AS metric, t.value AS value, t.ts AS ts, t.quality AS quality
        FROM telemetry_sample t
        JOIN metric m ON m.id = t.metric_id
        JOIN dev     ON dev.device_id = t.device_id
        WHERE dev.datacenter_id = CAST(:dc AS uuid)
          AND m.key IN ('outdoor_dry_bulb_temp', 'outdoor_wet_bulb_temp')
          AND t.ts > now() - interval '2 hours'
        ORDER BY m.key, t.ts DESC
    """), {"dc": datacenter_id})).mappings().all()
    return {r["metric"]: dict(r) for r in rows}


async def datacenter(session: AsyncSession, datacenter_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT id::text AS id, code, name, city, country, timezone,
               design_it_kw, design_pue, attributes
        FROM datacenter WHERE id = CAST(:dc AS uuid)
    """), {"dc": datacenter_id})).mappings().first()
    return dict(row) if row else None
