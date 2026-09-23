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

import json
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
           -- The ASHRAE class this room's intake air is graded against, and
           -- the rate limit that goes with it. NULL means unclassified, which
           -- is held to A1 and said out loud rather than passed off as a
           -- survey. See app/core/ashrae.py.
           rm.ashrae_class  AS ashrae_class,
           rm.max_rate_k_per_h AS max_rate_k_per_h,
           rm.datacenter_id AS datacenter_id,
           dc.code          AS site_code,
           dc.name          AS site_name
    FROM room rm
    JOIN datacenter dc ON dc.id = rm.datacenter_id
"""


async def thermal_rooms(session: AsyncSession) -> list[dict[str, Any]]:
    """Every room with its rack count - the skeleton the rack tier folds into.

    No readings here. Intake is a property of a rack (a probe on its door, or
    the servers in it), so a room's figure is its racks' figures added up,
    and a room with no racks is a row of dashes rather than a missing row.
    """
    rows = (await session.execute(text(f"""
        WITH rooms AS ({_ROOMS}),
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
               rooms.ashrae_class        AS ashrae_class,
               rooms.max_rate_k_per_h    AS max_rate_k_per_h,
               rooms.datacenter_id::text AS datacenter_id,
               rooms.site_code           AS site_code,
               rooms.site_name           AS site_name,
               COALESCE(racks.rack_count, 0) AS rack_count
        FROM rooms
        LEFT JOIN racks ON racks.room_id = rooms.room_id
        ORDER BY rooms.site_code, rooms.room_name
    """))).mappings().all()
    return [dict(r) for r in rows]


async def thermal_racks(session: AsyncSession, *, focus_start: datetime,
                        focus_end: datetime, compare_start: datetime,
                        compare_end: datetime, low_c: float,
                        high_c: float, allowable_c: float) -> list[dict[str, Any]]:
    """Rack intake from three sources, exhaust and humidity per rack, two windows.

    Three intake sources come back side by side and the service picks: the
    rack's environment probes (`ambient_temperature` on a racked device - the
    PDU's DPX-style probe hung at the front), the servers' BMC inlet, and the
    front-panel sensor of network gear. The probe is what a DCIM calls intake;
    the BMC is the fallback for a rack with no probe; the switch is the last
    resort for a rack with neither. All three are aggregated here so the
    choice is made once, in one place, with every set of counts in hand.

    The third source exists because a spine or management rack holds no
    servers and often no probe, so it read as a dash - no temperature at all -
    while the switches in it published a front-panel reading the whole time.
    Cisco, Arista and Juniper all expose one on the standard entity sensor
    table, and a rack of switches running hot is exactly what an operator
    needs to see. Ranked last because that sensor sits behind the bezel and
    reads a degree or two above the air the rack is really breathing.

    Every rack in inventory is a row, readings or not: a rack with no intake
    sensor is exactly the one an operator should notice. Sums and counts, not
    averages, so rooms and sites fold without averaging averages. Exhaust is a
    focus-window mean only - it exists to give ΔT. Ordered hottest first by
    whichever intake reading the rack has.

    EVERY FIGURE HERE IS PER SENSOR. `*_sum` is the sum of the sensors' own
    means over the window, not the sum of their readings, and `*_sensors` is
    what it divides by; `*_in_band`, `*_below` and `*_hot` are sensor TIME,
    each sensor's share of the window it spent in that band, divided by the
    same count. `*_n` survives as the raw reading count for one purpose: the
    Readings column, and the delta note that says a window was silent.

    The reason is that these sensors are not polled alike. A BMC inlet
    arrives about every 67 seconds on this estate and a rack probe about
    every 133, so pooling readings let a server-sourced rack cast twice the
    votes of a probe-sourced one for the same hour of air, and re-tuning a
    collector moved figures nobody had touched. A sensor polled every thirty
    seconds and one polled every five minutes now describe their hour with
    equal authority, which is what ASHRAE compliance is supposed to mean and
    what a mean temperature has always quietly claimed to be.
    """
    rows = (await session.execute(text("""
        WITH s AS (
            -- Intake (both windows, for the delta); exhaust and humidity
            -- (focus only - a delta of a ΔT helps nobody). Naming the window
            -- per key halves the raw scan on an uncompressed day.
            -- The network sensor is renamed on the way in. Downstream it is
            -- one more intake source among three; the metric it arrives as
            -- also carries power-supply and ASIC readings on other kit, and
            -- only the chassis instance on network gear is air.
            SELECT d.rack_id, t.device_id, t.instance, t.ts, t.value,
                   CASE WHEN m.key = 'component_temperature'
                        THEN 'network_intake' ELSE m.key END AS key
            FROM telemetry_sample t
            JOIN metric m ON m.id = t.metric_id
            JOIN device d ON d.id = t.device_id
                         AND d.rack_id IS NOT NULL
                         AND d.lifecycle <> 'decommissioned'
            LEFT JOIN device_type dt ON dt.code = d.device_type
            WHERE (m.key IN ('inlet_temperature', 'ambient_temperature')
                     AND ((t.ts >= :f0 AND t.ts < :f1)
                       OR (t.ts >= :c0 AND t.ts < :c1)))
               OR (m.key = 'component_temperature'
                     AND t.instance = 'CHASSIS'
                     AND dt.category = 'network'
                     AND ((t.ts >= :f0 AND t.ts < :f1)
                       OR (t.ts >= :c0 AND t.ts < :c1)))
               OR (m.key IN ('exhaust_temperature', 'relative_humidity')
                     AND t.ts >= :f0 AND t.ts < :f1)
        ),
        -- Per SENSOR first, then per rack. Two cheap hash aggregates instead
        -- of one count(DISTINCT device_id) that sorted half a million rows
        -- to disk.
        --
        -- A sensor is (device, metric, instance) - the same identity the
        -- rollups group on - and this level exists so the rack above can
        -- weight each sensor once. Pooling readings instead made the answer
        -- a function of the poll profile: a BMC inlet arrives about every 67
        -- seconds on this estate and a rack probe about every 133, so a
        -- server-sourced rack cast twice the votes of a probe-sourced one for
        -- the same hour of air, and re-tuning a collector moved a compliance
        -- figure nobody had touched.
        per_sensor AS (
            SELECT rack_id, device_id, instance, key,
                   sum(value) FILTER (WHERE ts >= :f0 AND ts < :f1) AS f_sum,
                   count(*)   FILTER (WHERE ts >= :f0 AND ts < :f1) AS f_n,
                   max(value) FILTER (WHERE ts >= :f0 AND ts < :f1) AS f_max,
                   count(*)   FILTER (WHERE ts >= :f0 AND ts < :f1
                                        AND value >= :low AND value <= :high) AS f_in_band,
                   -- The two ends of the distribution. Below the band is
                   -- overcooling, the finding a floor most often pays for;
                   -- above the allowable ceiling is where hardware is at
                   -- risk. Between them, above recommended is the remainder.
                   count(*)   FILTER (WHERE ts >= :f0 AND ts < :f1
                                        AND value < :low) AS f_below,
                   count(*)   FILTER (WHERE ts >= :f0 AND ts < :f1
                                        AND value > :allow) AS f_hot,
                   sum(value) FILTER (WHERE ts >= :c0 AND ts < :c1) AS c_sum,
                   count(*)   FILTER (WHERE ts >= :c0 AND ts < :c1) AS c_n,
                   max(value) FILTER (WHERE ts >= :c0 AND ts < :c1) AS c_max
            FROM s GROUP BY rack_id, device_id, instance, key
        ),
        agg AS (
            SELECT rack_id,
                   sum(f_sum / f_n)
                       FILTER (WHERE key = 'inlet_temperature' AND f_n > 0) AS f_sum,
                   sum(f_n)       FILTER (WHERE key = 'inlet_temperature') AS f_n,
                   max(f_max)     FILTER (WHERE key = 'inlet_temperature') AS f_max,
                   -- Time in band, per sensor, added up; `f_sensors` below is
                   -- what it divides by, so the rack's figure is the MEAN of
                   -- its sensors' time in band and not a pool of readings.
                   -- Each term is that sensor's own share of its own window,
                   -- which is its time in band whatever its cadence.
                   sum(f_in_band::double precision / f_n)
                       FILTER (WHERE key = 'inlet_temperature' AND f_n > 0) AS f_in_band,
                   sum(f_below::double precision / f_n)
                       FILTER (WHERE key = 'inlet_temperature' AND f_n > 0) AS f_below,
                   sum(f_hot::double precision / f_n)
                       FILTER (WHERE key = 'inlet_temperature' AND f_n > 0) AS f_hot,
                   count(*)       FILTER (WHERE key = 'inlet_temperature'
                                               AND f_n > 0) AS f_sensors,
                   sum(c_sum / c_n)
                       FILTER (WHERE key = 'inlet_temperature' AND c_n > 0) AS c_sum,
                   sum(c_n)       FILTER (WHERE key = 'inlet_temperature') AS c_n,
                   count(*)       FILTER (WHERE key = 'inlet_temperature'
                                               AND c_n > 0) AS c_sensors,
                   max(c_max)     FILTER (WHERE key = 'inlet_temperature') AS c_max,
                   sum(f_sum / f_n)
                       FILTER (WHERE key = 'ambient_temperature' AND f_n > 0) AS p_sum,
                   sum(f_n)       FILTER (WHERE key = 'ambient_temperature') AS p_n,
                   max(f_max)     FILTER (WHERE key = 'ambient_temperature') AS p_max,
                   sum(f_in_band::double precision / f_n)
                       FILTER (WHERE key = 'ambient_temperature' AND f_n > 0) AS p_in_band,
                   sum(f_below::double precision / f_n)
                       FILTER (WHERE key = 'ambient_temperature' AND f_n > 0) AS p_below,
                   sum(f_hot::double precision / f_n)
                       FILTER (WHERE key = 'ambient_temperature' AND f_n > 0) AS p_hot,
                   count(*)       FILTER (WHERE key = 'ambient_temperature'
                                               AND f_n > 0) AS p_sensors,
                   sum(c_sum / c_n)
                       FILTER (WHERE key = 'ambient_temperature' AND c_n > 0) AS pc_sum,
                   sum(c_n)       FILTER (WHERE key = 'ambient_temperature') AS pc_n,
                   count(*)       FILTER (WHERE key = 'ambient_temperature'
                                               AND c_n > 0) AS pc_sensors,
                   max(c_max)     FILTER (WHERE key = 'ambient_temperature') AS pc_max,
                   sum(f_sum / f_n)
                       FILTER (WHERE key = 'network_intake' AND f_n > 0) AS n_sum,
                   sum(f_n)       FILTER (WHERE key = 'network_intake') AS n_n,
                   max(f_max)     FILTER (WHERE key = 'network_intake') AS n_max,
                   sum(f_in_band::double precision / f_n)
                       FILTER (WHERE key = 'network_intake' AND f_n > 0) AS n_in_band,
                   sum(f_below::double precision / f_n)
                       FILTER (WHERE key = 'network_intake' AND f_n > 0) AS n_below,
                   sum(f_hot::double precision / f_n)
                       FILTER (WHERE key = 'network_intake' AND f_n > 0) AS n_hot,
                   count(*)       FILTER (WHERE key = 'network_intake'
                                               AND f_n > 0) AS n_sensors,
                   sum(c_sum / c_n)
                       FILTER (WHERE key = 'network_intake' AND c_n > 0) AS nc_sum,
                   sum(c_n)       FILTER (WHERE key = 'network_intake') AS nc_n,
                   count(*)       FILTER (WHERE key = 'network_intake'
                                               AND c_n > 0) AS nc_sensors,
                   max(c_max)     FILTER (WHERE key = 'network_intake') AS nc_max,
                   sum(f_sum / f_n)
                       FILTER (WHERE key = 'exhaust_temperature' AND f_n > 0) AS e_sum,
                   sum(f_n)       FILTER (WHERE key = 'exhaust_temperature') AS e_n,
                   count(*)       FILTER (WHERE key = 'exhaust_temperature'
                                               AND f_n > 0) AS e_sensors,
                   sum(f_sum / f_n)
                       FILTER (WHERE key = 'relative_humidity' AND f_n > 0) AS rh_sum,
                   sum(f_n)       FILTER (WHERE key = 'relative_humidity') AS rh_n,
                   max(f_max)     FILTER (WHERE key = 'relative_humidity') AS rh_max,
                   count(*)       FILTER (WHERE key = 'relative_humidity' AND f_n > 0) AS rh_probes
            FROM per_sensor GROUP BY rack_id
        )
        SELECT r.id::text            AS rack_id,
               r.name                AS rack_name,
               rr.name               AS row_name,
               r.u_height            AS u_height,
               rm.id::text           AS room_id,
               rm.name               AS room_name,
               rm.floor              AS floor,
               rm.room_class         AS room_class,
               dc.id::text           AS datacenter_id,
               dc.code               AS site_code,
               dc.name               AS site_name,
               agg.f_sum, agg.f_n, agg.f_max, agg.f_in_band, agg.f_sensors,
               agg.f_below, agg.f_hot,
               agg.p_sum, agg.p_n, agg.p_max, agg.p_in_band, agg.p_sensors,
               agg.p_below, agg.p_hot,
               agg.pc_sum, agg.pc_n, agg.pc_max, agg.pc_sensors,
               agg.n_sum, agg.n_n, agg.n_max, agg.n_in_band, agg.n_sensors,
               agg.n_below, agg.n_hot,
               agg.nc_sum, agg.nc_n, agg.nc_max, agg.nc_sensors,
               agg.e_sum, agg.e_n, agg.e_sensors,
               agg.c_sum, agg.c_n, agg.c_max, agg.c_sensors,
               agg.rh_sum, agg.rh_n, agg.rh_max, agg.rh_probes
        FROM rack r
        JOIN rack_row rr   ON rr.id = r.row_id
        JOIN room rm       ON rm.id = rr.room_id
        JOIN datacenter dc ON dc.id = rm.datacenter_id
        LEFT JOIN agg      ON agg.rack_id = r.id
        ORDER BY dc.code, rm.name,
                 COALESCE(agg.p_max, agg.f_max, agg.n_max) DESC NULLS LAST,
                 rr.ordinal, r.ordinal, r.name
    """), {"f0": focus_start, "f1": focus_end,
           "c0": compare_start, "c1": compare_end,
           "low": low_c, "high": high_c, "allow": allowable_c})).mappings().all()
    return [dict(r) for r in rows]


async def thermal_p90(session: AsyncSession, *, focus_start: datetime,
                      focus_end: datetime,
                      force: str = "") -> dict[str, Any]:
    """The 90th percentile SENSOR per rack, room, site and estate.

    A percentile cannot be folded from sums the way the averages are, so it
    is taken here over every tier in one pass: a room's p90 is the p90 of its
    racks' sensors, not a summary of their p90s. The source rule is applied
    first and per rack - the rack's front probes where any reported, else the
    servers' BMC inlet - so this reads the same population the averages and
    the in-band share do.

    Each sensor is collapsed to its own mean over the window BEFORE the
    percentile, so this ranks places rather than readings. Pooling readings
    ranked them partly by how often each sensor happened to be polled, and
    let one busy sensor's bad hour move a figure that exists precisely so
    that a single sensor cannot decide it - which Max is there to let happen.

    Interpolated (`percentile_cont`), focus window only: a delta of a
    percentile between two days is a number nobody acts on.

    Returns {"racks": {id: p90}, "rooms": {...}, "sites": {...},
    "total": p90 | None}; a tier with no readings is simply absent.
    """
    rows = (await session.execute(text("""
        WITH s AS (
            SELECT d.rack_id, t.device_id, t.instance, t.value,
                   CASE WHEN m.key = 'component_temperature'
                        THEN 'network_intake' ELSE m.key END AS key
            FROM telemetry_sample t
            JOIN metric m ON m.id = t.metric_id
            JOIN device d ON d.id = t.device_id
                         AND d.rack_id IS NOT NULL
                         AND d.lifecycle <> 'decommissioned'
            LEFT JOIN device_type dt ON dt.code = d.device_type
            WHERE (m.key IN ('inlet_temperature', 'ambient_temperature')
                   OR (m.key = 'component_temperature'
                       AND t.instance = 'CHASSIS'
                       AND dt.category = 'network'))
              AND t.ts >= :f0 AND t.ts < :f1
        ),
        -- One source per rack, in the same order the rows above choose in:
        -- probe, then server, then the switches' front panel. Pooling two
        -- sources would take a percentile over readings measured at
        -- different places.
        src AS (
            SELECT rack_id,
                   bool_or(key = 'ambient_temperature') AS has_probe,
                   bool_or(key = 'inlet_temperature')   AS has_server
            FROM s GROUP BY rack_id
        ),
        chosen AS (
            SELECT s.rack_id, s.device_id, s.instance, s.value
            FROM s JOIN src USING (rack_id)
            -- `force` pins every rack to one source, for a reader comparing
            -- what the probes say against what the servers say. Honoured HERE
            -- as well as in the rows: a percentile taken over a different
            -- population from the average beside it is two answers to one
            -- question.
            WHERE s.key = CASE
                      WHEN :force <> '' THEN :force
                      WHEN src.has_probe  THEN 'ambient_temperature'
                      WHEN src.has_server THEN 'inlet_temperature'
                      ELSE 'network_intake' END
        ),
        -- One row per sensor, at its own mean over the window, so the
        -- percentile ranks sensors and a sensor polled twice as often is not
        -- two of them.
        per_sensor AS (
            SELECT rack_id, avg(value) AS value
            FROM chosen GROUP BY rack_id, device_id, instance
        ),
        located AS (
            SELECT c.rack_id, rr.room_id, rm.datacenter_id, c.value
            FROM per_sensor c
            JOIN rack r      ON r.id  = c.rack_id
            JOIN rack_row rr ON rr.id = r.row_id
            JOIN room rm     ON rm.id = rr.room_id
        )
        SELECT rack_id::text       AS rack_id,
               room_id::text       AS room_id,
               datacenter_id::text AS datacenter_id,
               -- A percentile needs a population to rank. One sensor has no
               -- ninetieth of anything: taken over it, p90 is that sensor's
               -- own mean, which is the average printed beside it - and the
               -- two are rounded down different paths, so the column could
               -- show a p90 a tenth BELOW the average it was repeating.
               -- Most racks hold exactly one probe, so this is the common
               -- case at the rack tier, not an edge of it.
               CASE WHEN count(*) > 1
                    THEN percentile_cont(0.9) WITHIN GROUP (ORDER BY value)
               END AS p90
        FROM located
        GROUP BY GROUPING SETS ((rack_id), (room_id), (datacenter_id), ())
    """), {"f0": focus_start, "f1": focus_end,
           "force": force})).mappings().all()
    out: dict[str, Any] = {"racks": {}, "rooms": {}, "sites": {}, "total": None}
    for r in rows:
        p90 = None if r["p90"] is None else float(r["p90"])
        if r["rack_id"] is not None:
            out["racks"][r["rack_id"]] = p90
        elif r["room_id"] is not None:
            out["rooms"][r["room_id"]] = p90
        elif r["datacenter_id"] is not None:
            out["sites"][r["datacenter_id"]] = p90
        else:
            out["total"] = p90
    return out


# The rollups a trend may read, by the label the payload reports. The
# percentile per bucket is taken over the source rows, so it has one value
# per sensor per five minutes (or per hour) to work from - the time-weighted
# basis - rather than being a percentile of one number per sensor.
#: Where a trend point can be read from. The rollups refresh on a schedule -
#: five-minute every five minutes, hourly every thirty - so the newest bucket
#: of either is behind the plane by that much. `raw` is the hypertable itself,
#: which is current to the last poll and is what the newest bucket is redrawn
#: from.
_TREND_SOURCE = {"5m": "telemetry_5m", "1h": "telemetry_1h",
                 "raw": "telemetry_sample"}
_TREND_WIDTH = {"hour": timedelta(hours=1), "day": timedelta(days=1)}


async def thermal_trend(session: AsyncSession, *, start: datetime, end: datetime,
                        bucket: str, source: str, force: str = "",
                        room_id: str | None = None,
                        datacenter_id: str | None = None,
                        rack_id: str | None = None) -> list[dict[str, Any]]:
    """Intake average, p90 and max per bucket over a window, in one scope.

    The same readings the thermal table is made of, over time: per rack the
    front probes where any reported in the window, else the servers' BMC
    inlet, chosen once per rack for the whole window. Per bucket:

      avg  - the sample-weighted mean of the source rows, which is the mean
             of the readings;
      p90  - the 90th percentile of the source rows (one per sensor per five
             minutes, or per hour), interpolated;
      max  - the largest source max, i.e. the hottest reading.

    `source` is which table to read ("5m", "1h" or "raw"); the service picks
    it by window. The five-minute table's newest chunk stays uncompressed for
    days and its rows for two metrics are scattered across hundreds of
    megabytes of heap, so a week of it cold is a ten-second read; the hourly
    table is twelve times smaller for the same week. `raw` is the hypertable,
    used for the newest bucket only - a rollup cannot be more current than
    its refresh policy, and during an incident that is the one bucket
    somebody is watching.

    Buckets with nothing in them are absent; the service fills them so the
    line breaks where nothing was measured instead of bridging it.
    """
    table = _TREND_SOURCE[source]
    width = _TREND_WIDTH[bucket]
    # The hypertable keeps one reading per row; a rollup keeps the mean, the
    # maximum and how many readings are behind them. Named here so the rest of
    # the query does not care which it is reading.
    if source == "raw":
        stamp, mean, peak, weight = "t.ts", "t.value", "t.value", "1"
    else:
        stamp, mean, peak, weight = ("t.bucket", "t.avg_value", "t.max_value",
                                     "t.sample_count")
    scope = ""
    params: dict[str, Any] = {"t0": start, "t1": end, "width": width,
                              "force": force}
    if rack_id is not None:
        scope = "AND d.rack_id = CAST(:rack AS uuid)"
        params["rack"] = rack_id
    elif room_id is not None:
        scope = "AND rr.room_id = CAST(:room AS uuid)"
        params["room"] = room_id
    elif datacenter_id is not None:
        scope = "AND rm.datacenter_id = CAST(:site AS uuid)"
        params["site"] = datacenter_id
    rows = (await session.execute(text(f"""
        WITH s AS (
            SELECT d.rack_id, t.device_id, t.instance, m.key,
                   time_bucket(:width, {stamp}) AS b,
                   {mean} AS avg_value, {peak} AS max_value,
                   {weight} AS sample_count
            FROM {table} t
            JOIN metric m    ON m.id = t.metric_id
            JOIN device d    ON d.id = t.device_id
                            AND d.rack_id IS NOT NULL
                            AND d.lifecycle <> 'decommissioned'
            JOIN rack r      ON r.id  = d.rack_id
            JOIN rack_row rr ON rr.id = r.row_id
            JOIN room rm     ON rm.id = rr.room_id
            WHERE m.key IN ('inlet_temperature', 'ambient_temperature')
              AND {stamp} >= :t0 AND {stamp} < :t1
              {scope}
        ),
        src AS (
            SELECT rack_id, bool_or(key = 'ambient_temperature') AS has_probe
            FROM s GROUP BY rack_id
        ),
        -- `force` pins every rack to one source, the same way the table above
        -- the chart does. Without it a reader who pinned the tables to PROBES
        -- got a chart still drawn the automatic way, which is two different
        -- measurements of one estate on one screen with nothing saying so.
        chosen AS (
            SELECT s.b, s.device_id, s.instance, s.avg_value, s.max_value,
                   s.sample_count
            FROM s JOIN src USING (rack_id)
            WHERE s.key = CASE
                      WHEN :force <> '' THEN :force
                      WHEN src.has_probe THEN 'ambient_temperature'
                      ELSE 'inlet_temperature' END
        )
        SELECT b,
               sum(avg_value * sample_count) / NULLIF(sum(sample_count), 0) AS avg_c,
               percentile_cont(0.9) WITHIN GROUP (ORDER BY avg_value)       AS p90_c,
               max(max_value)                                               AS max_c,
               -- A sensor is (device, instance), which is what the table
               -- and the compliance trend both weight by. Counting devices
               -- undercounted every rack whose probe reports two instances,
               -- and the two charts sat side by side saying 150 and 154 of
               -- one population.
               count(DISTINCT (device_id, instance))                         AS sensors
        FROM chosen
        GROUP BY b
        ORDER BY b
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def thermal_compliance_trend(session: AsyncSession, *, start: datetime,
                                   end: datetime, bucket: str, source: str,
                                   low_c: float, high_c: float, allowable_c: float,
                                   force: str = "",
                                   room_id: str | None = None,
                                   datacenter_id: str | None = None,
                                   rack_id: str | None = None) -> list[dict[str, Any]]:
    """The four-way ASHRAE split per bucket over a window, in one scope.

    The table's spread bar is one window wide; this is the same partition per
    hour or per day, which is what says whether a setpoint change stuck and
    how much of a floor has been overcooled all week rather than this
    afternoon. Same source rows and the same probe-first rack rule as
    `thermal_trend`, so the two cells of the chart's view control are two
    readings of one measurement and never of two.

    PER SENSOR, like every other figure on this page. Each sensor's share of
    its own bucket is worked out first and the bucket is the MEAN of those
    shares, so a BMC polled every 67 seconds and a rack probe polled every
    133 describe their hour with equal authority. Pooling the rows instead
    would let the densely polled racks carry the estate's compliance figure,
    which is the bug the table was rebuilt to remove - it would be a poll
    profile drawn as a temperature.

    A sensor is (device, instance), inside a rack already pinned to one
    metric by `chosen` - the same identity the table weights by.

    Resolution follows `source`, exactly as the intake line's p90 does: from
    the five-minute rollup a sensor's hour is twelve classifications, from
    the hourly one a day is twenty-four, and from `raw` it is every reading.
    A sub-bucket is classified by its MEAN, so a five-minute period that
    crossed the line reads as wherever it spent most of itself; at this
    resolution that is a rounding of minutes, not of hours.

    The four shares partition the bucket: below the recommended floor, inside
    it, above it but allowable, above the allowable ceiling. Buckets with
    nothing in them are absent; the service fills them.
    """
    table = _TREND_SOURCE[source]
    width = _TREND_WIDTH[bucket]
    if source == "raw":
        stamp, value = "t.ts", "t.value"
    else:
        stamp, value = "t.bucket", "t.avg_value"
    scope = ""
    params: dict[str, Any] = {"t0": start, "t1": end, "width": width,
                              "force": force, "low": low_c, "high": high_c,
                              "allow": allowable_c}
    if rack_id is not None:
        scope = "AND d.rack_id = CAST(:rack AS uuid)"
        params["rack"] = rack_id
    elif room_id is not None:
        scope = "AND rr.room_id = CAST(:room AS uuid)"
        params["room"] = room_id
    elif datacenter_id is not None:
        scope = "AND rm.datacenter_id = CAST(:site AS uuid)"
        params["site"] = datacenter_id
    rows = (await session.execute(text(f"""
        WITH s AS (
            SELECT d.rack_id, t.device_id, t.instance, m.key,
                   time_bucket(:width, {stamp}) AS b,
                   {value} AS value
            FROM {table} t
            JOIN metric m    ON m.id = t.metric_id
            JOIN device d    ON d.id = t.device_id
                            AND d.rack_id IS NOT NULL
                            AND d.lifecycle <> 'decommissioned'
            JOIN rack r      ON r.id  = d.rack_id
            JOIN rack_row rr ON rr.id = r.row_id
            JOIN room rm     ON rm.id = rr.room_id
            WHERE m.key IN ('inlet_temperature', 'ambient_temperature')
              AND {stamp} >= :t0 AND {stamp} < :t1
              {scope}
        ),
        src AS (
            SELECT rack_id, bool_or(key = 'ambient_temperature') AS has_probe
            FROM s GROUP BY rack_id
        ),
        chosen AS (
            SELECT s.b, s.device_id, s.instance, s.value
            FROM s JOIN src USING (rack_id)
            WHERE s.key = CASE
                      WHEN :force <> '' THEN :force
                      WHEN src.has_probe THEN 'ambient_temperature'
                      ELSE 'inlet_temperature' END
              AND s.value IS NOT NULL
        ),
        -- Each sensor's own bucket, as four shares of itself.
        per_sensor AS (
            SELECT b, device_id, instance,
                   count(*) FILTER (WHERE value < :low)::double precision
                       / count(*) AS below,
                   count(*) FILTER (WHERE value >= :low AND value <= :high)::double precision
                       / count(*) AS in_band,
                   count(*) FILTER (WHERE value > :high AND value <= :allow)::double precision
                       / count(*) AS warm,
                   count(*) FILTER (WHERE value > :allow)::double precision
                       / count(*) AS hot
            FROM chosen GROUP BY b, device_id, instance
        )
        SELECT b,
               avg(below)   AS below,
               avg(in_band) AS in_band,
               avg(warm)    AS warm,
               avg(hot)     AS hot,
               count(*)     AS sensors
        FROM per_sensor
        GROUP BY b
        ORDER BY b
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def thermal_delta_trend(session: AsyncSession, *, start: datetime,
                             end: datetime, bucket: str, source: str,
                             force: str = "", room_id: str | None = None,
                             rack_id: str | None = None) -> list[dict[str, Any]]:
    """Intake, exhaust and the gap between them per bucket, in ONE room or rack.

    The table's ΔT column over time. Intake is the page's probe-first rack
    source; exhaust is `exhaust_temperature`, which only servers report -
    nothing else in a rack has a sensor in its own discharge. ΔT is the
    difference of the two means, exactly as the table takes it, so the line
    and the column cannot disagree.

    What it is for: ΔT is the only airflow diagnosis on this page that intake
    temperature structurally cannot make. A rack breathing 23 C and
    discharging 31 C is moving its heat; the same rack discharging 26 C at
    the same intake is being flushed with air that never crossed a server -
    bypass - and the cooling plant is paying fan power to move it. Rising ΔT
    at flat intake is containment working; falling ΔT at flat intake is
    containment leaking. Both look identical in an intake reading.

    ROOM OR RACK ONLY, enforced by the service. A mean over halls describes
    no air path that exists: two rooms with opposite problems average to a
    healthy number, and the estate has no single supply-to-return path to
    take a difference along.

    Per SENSOR on both legs, weighted once each, the same as everything else
    on this page - and the same as the table, whose rooms fold from their
    racks' sensor sums rather than from rack averages. Racks with no server
    in them contribute intake and no exhaust; they are counted separately so
    a caption can say how much of the room the ΔT speaks for.
    """
    table = _TREND_SOURCE[source]
    width = _TREND_WIDTH[bucket]
    if source == "raw":
        stamp, value, weight = "t.ts", "t.value", "1"
    else:
        stamp, value, weight = "t.bucket", "t.avg_value", "t.sample_count"
    scope = ""
    params: dict[str, Any] = {"t0": start, "t1": end, "width": width,
                              "force": force}
    if rack_id is not None:
        scope = "AND d.rack_id = CAST(:rack AS uuid)"
        params["rack"] = rack_id
    else:
        scope = "AND rr.room_id = CAST(:room AS uuid)"
        params["room"] = room_id
    rows = (await session.execute(text(f"""
        WITH s AS (
            SELECT d.rack_id, t.device_id, t.instance, m.key,
                   time_bucket(:width, {stamp}) AS b,
                   {value} AS value, {weight} AS weight
            FROM {table} t
            JOIN metric m    ON m.id = t.metric_id
            JOIN device d    ON d.id = t.device_id
                            AND d.rack_id IS NOT NULL
                            AND d.lifecycle <> 'decommissioned'
            JOIN rack r      ON r.id  = d.rack_id
            JOIN rack_row rr ON rr.id = r.row_id
            WHERE m.key IN ('inlet_temperature', 'ambient_temperature',
                            'exhaust_temperature')
              AND {stamp} >= :t0 AND {stamp} < :t1
              {scope}
        ),
        -- Which intake source each rack is pinned to, decided over the whole
        -- window as the table and the other trends decide it. The exhaust leg
        -- takes no part in the decision.
        src AS (
            SELECT rack_id, bool_or(key = 'ambient_temperature') AS has_probe
            FROM s WHERE key <> 'exhaust_temperature' GROUP BY rack_id
        ),
        -- Each sensor's own mean for its own bucket, sample-weighted so a
        -- rollup row standing for twelve readings counts as twelve.
        per_sensor AS (
            SELECT s.b, s.rack_id, s.device_id, s.instance,
                   CASE WHEN s.key = 'exhaust_temperature'
                        THEN 'exhaust' ELSE 'intake' END AS leg,
                   sum(s.value * s.weight) / NULLIF(sum(s.weight), 0) AS value
            FROM s LEFT JOIN src USING (rack_id)
            WHERE s.key = 'exhaust_temperature'
               OR s.key = CASE
                      WHEN :force <> '' THEN :force
                      WHEN src.has_probe THEN 'ambient_temperature'
                      ELSE 'inlet_temperature' END
            GROUP BY s.b, s.rack_id, s.device_id, s.instance, leg
        )
        SELECT b,
               avg(value) FILTER (WHERE leg = 'intake')   AS intake_c,
               avg(value) FILTER (WHERE leg = 'exhaust')  AS exhaust_c,
               count(*)   FILTER (WHERE leg = 'intake')   AS intake_sensors,
               count(*)   FILTER (WHERE leg = 'exhaust')  AS exhaust_sensors,
               count(DISTINCT rack_id) FILTER (WHERE leg = 'exhaust') AS racks
        FROM per_sensor
        GROUP BY b
        ORDER BY b
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def thermal_envelope(session: AsyncSession, *, start: datetime,
                           end: datetime, limits: list[dict[str, Any]],
                           max_gap_minutes: int = 30,
                           moisture_bucket_minutes: int = 5) -> list[dict[str, Any]]:
    """The full ASHRAE envelope per rack over a window: three legs, co-timed.

    The table above this one scores DRY BULB. ASHRAE writes three limits, and
    the other two are what this adds:

      MOISTURE  a dew-point floor, a dew-point ceiling and a humidity ceiling.
                Both ceilings bind and the worse one wins - at 20 C, 60 % RH is
                a 12.0 C dew point and the humidity limit bites first; at 27 C
                the same 60 % is 18.6 C and the dew point bites four kelvin
                earlier. A page checking only RH passes a hall ASHRAE fails.
      RATE      how fast the intake is moving, in K/hour. Thermal shock and
                condensation damage hardware whether or not the air ever left
                the band, and a hall at 26 C holding flat and one at 26 C
                climbing 30 K/hour need different answers in the same minute.

    MOISTURE IS A PROPERTY OF THE ROOM, not of the rack. Air in a hall mixes;
    humidity varies little across it, which is why nobody fits a humidity
    element to every rack and why ASHRAE writes moisture as a condition of the
    SPACE. If a hall's air is at 70 % RH then every rack in it is breathing
    70 % RH, whether or not that rack owns a probe.

    Scoring it per rack instead - which this did first - let the failure
    vanish. A hall of 44 sensors with 6 humidity probes had its moisture
    breach averaged against 38 sensors that cannot measure moisture and
    therefore "passed": a real 99.4 % moisture reading became a 99.96 %
    envelope, which prints as 100.0. The estate fold diluted it a second time.
    A leg no sensor can measure must not be able to vote that the leg passed.

    So the room's moisture state is decided per five-minute bucket from the
    room's own probes, and every sensor in that room inherits it. Inside a
    bucket the WORST probe wins: a compliance claim is a statement that the
    space was inside the envelope, and a monitored point saying otherwise has
    to be answered with air or with a repair, not with an average that hides
    it. A room with no humidity probe at all has no moisture leg, which is
    missing evidence rather than a pass.

    CO-TIMED, which is the rest of the difficulty. An envelope grade is a
    statement about one instant - "this sensor was inside all three limits at
    this moment" - so the moisture state is matched to the bucket each
    temperature reading falls in rather than averaged over the window.

    Dew point is DERIVED rather than polled. A DPX2 is a thermistor and a
    humidity element on an RJ-12 lead; no Raritan firmware will give you a dew
    point for it, and the parts that do publish one (Geist, some Vertiv heads)
    are a minority here. Magnus-Tetens with the Sonntag coefficients, the same
    relation the simulator generates its probes with, so the two sides agree
    to better than 0.1 K.

    `limits` carries one row per ROOM - its class's numbers, resolved in the
    service - because a hall of A3 kit is allowed to 40 C by the same standard
    that stops A1 at 32, and grading both against A1 puts an alarm on
    equipment running inside its own specification.

    Rate is taken over FIFTEEN-MINUTE MEANS rather than consecutive readings.
    Between polls it measures the sensor: a probe carries about 0.3 K of
    sample noise and is read every 30 to 130 seconds, so a third of a degree
    across half a minute divides out to 36 K/hour. Measured on this estate the
    first attempt graded healthy halls at 21-32 K/hour peaks and failed a
    tenth of every window - all of it jitter. Fifteen minutes is the baseline
    the page's NOW rate already uses, and a pair further apart than
    `max_gap_minutes` is an absence rather than a drift.

    Every rack row carries its ROOM's moisture figures as well as its own, so
    the service can report the leg once per room rather than once per rack and
    weight it by the sensors the verdict was applied to.
    """
    rows = (await session.execute(text("""
        WITH lim AS (
            SELECT (l ->> 'room_id')::uuid            AS room_id,
                   (l ->> 'rec_low')::float           AS rec_low,
                   (l ->> 'rec_high')::float          AS rec_high,
                   (l ->> 'allow_low')::float         AS allow_low,
                   (l ->> 'allow_high')::float        AS allow_high,
                   (l ->> 'rec_dp_low')::float        AS rec_dp_low,
                   (l ->> 'rec_dp_high')::float       AS rec_dp_high,
                   (l ->> 'rec_rh_high')::float       AS rec_rh_high,
                   (l ->> 'allow_dp_low')::float      AS allow_dp_low,
                   (l ->> 'allow_dp_high')::float     AS allow_dp_high,
                   (l ->> 'allow_rh_low')::float      AS allow_rh_low,
                   (l ->> 'allow_rh_high')::float     AS allow_rh_high,
                   (l ->> 'max_rate')::float          AS max_rate
            FROM jsonb_array_elements(CAST(:limits AS jsonb)) AS l
        ),
        -- One row per sensor per POLL, with whatever that poll carried. The
        -- pivot is what lets a probe's humidity be read beside the temperature
        -- it was measured with rather than against some other sensor's.
        reading AS (
            SELECT d.rack_id, rr.room_id, t.device_id, t.instance, t.ts,
                   max(t.value) FILTER (WHERE m.key = 'ambient_temperature') AS probe_c,
                   max(t.value) FILTER (WHERE m.key = 'inlet_temperature')   AS bmc_c,
                   max(t.value) FILTER (WHERE m.key = 'relative_humidity')   AS rh_pct,
                   max(t.value) FILTER (WHERE m.key = 'dew_point')           AS dp_pub
            FROM telemetry_sample t
            JOIN metric m    ON m.id = t.metric_id
            JOIN device d    ON d.id = t.device_id
                            AND d.rack_id IS NOT NULL
                            AND d.lifecycle <> 'decommissioned'
            JOIN rack r      ON r.id  = d.rack_id
            JOIN rack_row rr ON rr.id = r.row_id
            WHERE m.key IN ('ambient_temperature', 'inlet_temperature',
                            'relative_humidity', 'dew_point')
              AND t.ts >= :t0 AND t.ts < :t1
              -- A room transmitter on a wall is not rack intake air. Migration
              -- 0063 split the alarm rules on this same instance for the same
              -- reason: the envelope is written for what equipment breathes.
              AND t.instance <> 'ROOM'
            GROUP BY d.rack_id, rr.room_id, t.device_id, t.instance, t.ts
        ),
        -- The page's probe-first rule, decided once per rack over the whole
        -- window, exactly as the table and the trends decide it.
        src AS (
            SELECT rack_id, bool_or(probe_c IS NOT NULL) AS has_probe
            FROM reading GROUP BY rack_id
        ),
        chosen AS (
            SELECT r.*, CASE WHEN s.has_probe THEN r.probe_c ELSE r.bmc_c END AS temp_c
            FROM reading r JOIN src s USING (rack_id)
        ),
        -- Every moisture reading in the estate, graded. Dew point published
        -- where a probe has one, else Magnus from this same reading's
        -- temperature and humidity.
        moisture AS (
            SELECT c.room_id,
                   time_bucket(make_interval(mins => :moist_bucket), c.ts) AS b,
                   GREATEST(
                     CASE WHEN dp.v IS NULL THEN 0
                          WHEN dp.v BETWEEN l.rec_dp_low AND l.rec_dp_high THEN 0
                          WHEN dp.v BETWEEN l.allow_dp_low AND l.allow_dp_high THEN 1
                          ELSE 2 END,
                     CASE WHEN c.rh_pct IS NULL THEN 0
                          WHEN c.rh_pct <= l.rec_rh_high THEN 0
                          WHEN c.rh_pct BETWEEN l.allow_rh_low AND l.allow_rh_high THEN 1
                          ELSE 2 END) AS g,
                   c.device_id, c.instance
            FROM chosen c
            JOIN lim l ON l.room_id = c.room_id
            LEFT JOIN LATERAL (
                SELECT CASE
                         WHEN c.dp_pub IS NOT NULL THEN c.dp_pub
                         WHEN c.probe_c IS NOT NULL AND c.rh_pct IS NOT NULL THEN
                           243.12 * (ln(GREATEST(c.rh_pct, 0.5) / 100.0)
                                     + (17.62 * c.probe_c) / (243.12 + c.probe_c))
                           / (17.62 - (ln(GREATEST(c.rh_pct, 0.5) / 100.0)
                                       + (17.62 * c.probe_c) / (243.12 + c.probe_c)))
                       END AS v
            ) dp ON true
            WHERE c.rh_pct IS NOT NULL OR c.dp_pub IS NOT NULL
        ),
        -- The room's moisture state per bucket: the WORST probe in it. One
        -- monitored point outside the envelope is the room outside the
        -- envelope until somebody answers it with air or with a repair.
        room_state AS (
            SELECT room_id, b, max(g) AS g_moist
            FROM moisture GROUP BY room_id, b
        ),
        -- And the room's own moisture figure, for the page: the share of its
        -- buckets that were in band, with how many probes spoke for it.
        room_moist AS (
            SELECT rs.room_id,
                   avg(CASE WHEN rs.g_moist = 0 THEN 1.0 ELSE 0.0 END) AS moist_ok,
                   count(*) AS moist_buckets,
                   (SELECT count(DISTINCT (m2.device_id, m2.instance))
                    FROM moisture m2 WHERE m2.room_id = rs.room_id) AS probes
            FROM room_state rs GROUP BY rs.room_id
        ),
        -- Rate, over fifteen-minute means of one sensor.
        bucketed AS (
            SELECT device_id, instance,
                   time_bucket(INTERVAL '15 minutes', ts) AS b,
                   avg(temp_c) AS temp_c
            FROM chosen WHERE temp_c IS NOT NULL
            GROUP BY device_id, instance, time_bucket(INTERVAL '15 minutes', ts)
        ),
        paced AS (
            SELECT device_id, instance, b,
                   (temp_c - lag(temp_c) OVER w)
                     / NULLIF(EXTRACT(EPOCH FROM (b - lag(b) OVER w)) / 3600.0, 0)
                     AS k_per_h,
                   EXTRACT(EPOCH FROM (b - lag(b) OVER w)) / 60.0 AS gap_min
            FROM bucketed
            WINDOW w AS (PARTITION BY device_id, instance ORDER BY b)
        ),
        -- Each reading, with its own temperature grade, its ROOM's moisture
        -- state for the bucket it falls in, and the rate its own fifteen
        -- minutes were moving at.
        graded AS (
            SELECT c.rack_id, c.room_id, c.device_id, c.instance,
                   CASE WHEN c.temp_c BETWEEN l.rec_low AND l.rec_high THEN 0
                        WHEN c.temp_c BETWEEN l.allow_low AND l.allow_high THEN 1
                        ELSE 2 END AS g_temp,
                   rs.g_moist AS g_moist,
                   CASE WHEN p.k_per_h IS NULL OR p.gap_min > :max_gap THEN NULL
                        WHEN abs(p.k_per_h) <= l.max_rate THEN 0
                        ELSE 2 END AS g_rate,
                   CASE WHEN p.gap_min > :max_gap THEN NULL ELSE p.k_per_h END AS k_per_h
            FROM chosen c
            JOIN lim l ON l.room_id = c.room_id
            LEFT JOIN room_state rs
                   ON rs.room_id = c.room_id
                  AND rs.b = time_bucket(make_interval(mins => :moist_bucket), c.ts)
            LEFT JOIN paced p ON p.device_id = c.device_id
                             AND p.instance = c.instance
                             AND p.b = time_bucket(INTERVAL '15 minutes', c.ts)
            WHERE c.temp_c IS NOT NULL
        ),
        -- Per SENSOR first: each sensor's own share of its own window, so a
        -- probe polled every 133 s and a BMC polled every 67 s describe their
        -- hour with equal authority. The tier above sums these.
        per_sensor AS (
            SELECT rack_id, room_id, device_id, instance,
                   avg(CASE WHEN g_temp = 0 THEN 1.0 ELSE 0.0 END) AS temp_ok,
                   avg(CASE WHEN g_rate = 0 THEN 1.0 ELSE 0.0 END)
                     FILTER (WHERE g_rate IS NOT NULL)             AS rate_ok,
                   avg(CASE WHEN GREATEST(g_temp, coalesce(g_moist, 0),
                                          coalesce(g_rate, 0)) = 0
                            THEN 1.0 ELSE 0.0 END)                 AS env_ok,
                   count(*) FILTER (WHERE g_moist IS NOT NULL) AS moist_n,
                   count(*) FILTER (WHERE g_rate IS NOT NULL)  AS rate_n,
                   max(abs(k_per_h)) FILTER (WHERE g_rate IS NOT NULL) AS peak_rate
            FROM graded
            GROUP BY rack_id, room_id, device_id, instance
        )
        -- SUMS and COUNTS, not averages: a room folds from its racks and a
        -- site from its rooms, and an average of averages would weight a rack
        -- with one probe like a rack with four. Each term is one sensor's own
        -- share of its own window, so the divisor is always a sensor count.
        --
        -- The room's moisture figure rides along unaggregated: it belongs to
        -- the room, and the service reports it once rather than summing it per
        -- rack.
        SELECT ps.rack_id,
               ps.room_id::text AS room_id,
               sum(ps.temp_ok)  AS temp_sum,
               sum(ps.rate_ok)  FILTER (WHERE ps.rate_ok IS NOT NULL) AS rate_sum,
               sum(ps.env_ok)   AS env_sum,
               count(*)                                      AS sensors,
               count(*) FILTER (WHERE ps.rate_ok IS NOT NULL) AS rate_sensors,
               count(*) FILTER (WHERE ps.moist_n > 0)         AS moisture_graded,
               max(ps.peak_rate)                             AS peak_rate,
               max(rm.moist_ok)                              AS room_moist_ok,
               max(rm.probes)                                AS room_probes,
               max(rm.moist_buckets)                         AS room_moist_buckets
        FROM per_sensor ps
        LEFT JOIN room_moist rm ON rm.room_id = ps.room_id
        GROUP BY ps.rack_id, ps.room_id
    """), {"t0": start, "t1": end, "limits": json.dumps(limits),
           "max_gap": max_gap_minutes,
           "moist_bucket": moisture_bucket_minutes})).mappings().all()
    return [dict(r) for r in rows]


#: How far back a NOW reading may have come from.
#:
#: One poll interval is not enough: a rack probe is polled every two to four
#: minutes and a server every one to three, and a sensor that happened to be
#: read a moment before the horizon would drop out of the estate and take its
#: rack with it. Ten minutes covers the slowest cadence several times over
#: while still excluding a sensor that has genuinely stopped.
NOW_HORIZON = timedelta(minutes=10)

#: How far back the NOW view looks to say which way the air is going.
#:
#: Long enough to clear the sensors: consecutive readings on a settled floor
#: move 0.1-0.3 K on noise alone, which is the size of a real event's first
#: minutes, so a rate taken between two samples would flicker constantly and
#: be least trustworthy exactly when it mattered. Short enough to catch a
#: cooling loss while it is still developing.
#:
#: Fixed, and the same for every row. A rate measured between "the last two
#: readings" would be a two-minute change on a rack read by a 120 s probe and
#: a one-minute change on the rack beneath it read by 60 s BMCs - two
#: different measurements under one heading.
RATE_WINDOW = timedelta(minutes=15)


async def thermal_racks_now(session: AsyncSession, *, since: datetime,
                            was_at: datetime, low_c: float, high_c: float,
                            allowable_c: float) -> list[dict[str, Any]]:
    """The newest reading from every rack sensor, right now.

    Same shape as `thermal_racks` so the service folds it identically, and the
    same probe-first source rule applies on top. What differs is what a row
    counts: one reading per SENSOR rather than every reading over a window, so
    the in-band share is the share of sensors in band at this instant rather
    than the share of time they spent there over an hour.

    A sensor is (device, metric, instance), the same identity the windowed
    query weights by, so one sensor is one vote in both and the two views
    cannot disagree about what the population is. This view has always been
    per sensor - an instant has no time to weight - which is why it needed no
    change when the windowed figures stopped pooling readings.

    That is the number an operator wants while something is happening. An
    hour's mean cannot show a step change until an hour has passed, and holds
    it for an hour after it clears - so a tripped CRAH looked like nothing for
    ten minutes and like a fault long after it was fixed.

    A second snapshot comes back with it, taken the same way as of
    RATE_WINDOW ago, and the service turns the pair into a rate of change.
    Same estimator on both ends - newest reading per sensor - so the interval
    is the fifteen minutes between them and not an artifact of how often a
    given rack happens to be polled. A sensor that has no reading that old
    contributes to neither end, which is what keeps a probe fitted this
    morning from reading as a plunge.
    """
    rows = (await session.execute(text("""
        WITH latest AS (
            SELECT DISTINCT ON (t.device_id, t.metric_id, t.instance)
                   t.device_id, t.value,
                   CASE WHEN m.key = 'component_temperature'
                        THEN 'network_intake' ELSE m.key END AS key
            FROM telemetry_sample t
            JOIN metric m ON m.id = t.metric_id
            JOIN device d ON d.id = t.device_id
                         AND d.rack_id IS NOT NULL
                         AND d.lifecycle <> 'decommissioned'
            LEFT JOIN device_type dt ON dt.code = d.device_type
            WHERE (m.key IN ('inlet_temperature', 'ambient_temperature',
                             'exhaust_temperature', 'relative_humidity')
                   OR (m.key = 'component_temperature'
                       AND t.instance = 'CHASSIS'
                       AND dt.category = 'network'))
              AND t.ts >= :t0
            ORDER BY t.device_id, t.metric_id, t.instance, t.ts DESC
        ),
        -- The same shot, taken as of RATE_WINDOW ago. Bounded below as well
        -- as above: the newest reading BEFORE the mark, but not one from an
        -- hour before it, or a sensor that has since gone quiet would supply
        -- a stale far end and the rate would describe a gap in collection
        -- rather than a change in the air.
        earlier AS (
            SELECT DISTINCT ON (t.device_id, t.metric_id, t.instance)
                   t.device_id, t.value,
                   CASE WHEN m.key = 'component_temperature'
                        THEN 'network_intake' ELSE m.key END AS key
            FROM telemetry_sample t
            JOIN metric m ON m.id = t.metric_id
            JOIN device d ON d.id = t.device_id
                         AND d.rack_id IS NOT NULL
                         AND d.lifecycle <> 'decommissioned'
            LEFT JOIN device_type dt ON dt.code = d.device_type
            WHERE (m.key IN ('inlet_temperature', 'ambient_temperature')
                   OR (m.key = 'component_temperature'
                       AND t.instance = 'CHASSIS'
                       AND dt.category = 'network'))
              AND t.ts <= :was AND t.ts >= :was_floor
            ORDER BY t.device_id, t.metric_id, t.instance, t.ts DESC
        ),
        per_rack_was AS (
            SELECT d.rack_id, e.key,
                   sum(e.value) AS w_sum,
                   count(*)     AS w_n,
                   max(e.value) AS w_max
            FROM earlier e JOIN device d ON d.id = e.device_id
            GROUP BY d.rack_id, e.key
        ),
        was AS (
            SELECT rack_id,
                   sum(w_sum) FILTER (WHERE key = 'inlet_temperature')   AS c_sum,
                   sum(w_n)   FILTER (WHERE key = 'inlet_temperature')   AS c_n,
                   sum(w_n)   FILTER (WHERE key = 'inlet_temperature')   AS c_sensors,
                   max(w_max) FILTER (WHERE key = 'inlet_temperature')   AS c_max,
                   sum(w_sum) FILTER (WHERE key = 'ambient_temperature') AS pc_sum,
                   sum(w_n)   FILTER (WHERE key = 'ambient_temperature') AS pc_n,
                   sum(w_n)   FILTER (WHERE key = 'ambient_temperature') AS pc_sensors,
                   max(w_max) FILTER (WHERE key = 'ambient_temperature') AS pc_max,
                   sum(w_sum) FILTER (WHERE key = 'network_intake')      AS nc_sum,
                   sum(w_n)   FILTER (WHERE key = 'network_intake')      AS nc_n,
                   sum(w_n)   FILTER (WHERE key = 'network_intake')      AS nc_sensors,
                   max(w_max) FILTER (WHERE key = 'network_intake')      AS nc_max
            FROM per_rack_was GROUP BY rack_id
        ),
        per_rack AS (
            SELECT d.rack_id, l.key,
                   sum(l.value)                                     AS v_sum,
                   count(*)                                         AS v_n,
                   max(l.value)                                     AS v_max,
                   count(*) FILTER (WHERE l.value >= :low
                                      AND l.value <= :high)         AS v_in_band,
                   count(*) FILTER (WHERE l.value < :low)           AS v_below,
                   count(*) FILTER (WHERE l.value > :allow)         AS v_hot
            FROM latest l JOIN device d ON d.id = l.device_id
            GROUP BY d.rack_id, l.key
        ),
        agg AS (
            SELECT rack_id,
                   sum(v_sum)      FILTER (WHERE key = 'inlet_temperature')   AS f_sum,
                   sum(v_n)        FILTER (WHERE key = 'inlet_temperature')   AS f_n,
                   max(v_max)      FILTER (WHERE key = 'inlet_temperature')   AS f_max,
                   sum(v_in_band)  FILTER (WHERE key = 'inlet_temperature')   AS f_in_band,
                   sum(v_below)    FILTER (WHERE key = 'inlet_temperature')   AS f_below,
                   sum(v_hot)      FILTER (WHERE key = 'inlet_temperature')   AS f_hot,
                   sum(v_n)        FILTER (WHERE key = 'inlet_temperature')   AS f_sensors,
                   sum(v_sum)      FILTER (WHERE key = 'ambient_temperature') AS p_sum,
                   sum(v_n)        FILTER (WHERE key = 'ambient_temperature') AS p_n,
                   max(v_max)      FILTER (WHERE key = 'ambient_temperature') AS p_max,
                   sum(v_in_band)  FILTER (WHERE key = 'ambient_temperature') AS p_in_band,
                   sum(v_below)    FILTER (WHERE key = 'ambient_temperature') AS p_below,
                   sum(v_hot)      FILTER (WHERE key = 'ambient_temperature') AS p_hot,
                   sum(v_n)        FILTER (WHERE key = 'ambient_temperature') AS p_sensors,
                   sum(v_sum)      FILTER (WHERE key = 'network_intake')      AS n_sum,
                   sum(v_n)        FILTER (WHERE key = 'network_intake')      AS n_n,
                   max(v_max)      FILTER (WHERE key = 'network_intake')      AS n_max,
                   sum(v_in_band)  FILTER (WHERE key = 'network_intake')      AS n_in_band,
                   sum(v_below)    FILTER (WHERE key = 'network_intake')      AS n_below,
                   sum(v_hot)      FILTER (WHERE key = 'network_intake')      AS n_hot,
                   sum(v_n)        FILTER (WHERE key = 'network_intake')      AS n_sensors,
                   sum(v_sum)      FILTER (WHERE key = 'exhaust_temperature') AS e_sum,
                   sum(v_n)        FILTER (WHERE key = 'exhaust_temperature') AS e_n,
                   sum(v_n)        FILTER (WHERE key = 'exhaust_temperature') AS e_sensors,
                   sum(v_sum)      FILTER (WHERE key = 'relative_humidity')   AS rh_sum,
                   sum(v_n)        FILTER (WHERE key = 'relative_humidity')   AS rh_n,
                   max(v_max)      FILTER (WHERE key = 'relative_humidity')   AS rh_max,
                   sum(v_n)        FILTER (WHERE key = 'relative_humidity')   AS rh_probes
            FROM per_rack GROUP BY rack_id
        )
        SELECT r.id::text            AS rack_id,
               r.name                AS rack_name,
               rr.name               AS row_name,
               r.u_height            AS u_height,
               rm.id::text           AS room_id,
               rm.name               AS room_name,
               rm.floor              AS floor,
               rm.room_class         AS room_class,
               dc.id::text           AS datacenter_id,
               dc.code               AS site_code,
               dc.name               AS site_name,
               agg.f_sum, agg.f_n, agg.f_max, agg.f_in_band, agg.f_sensors,
               agg.f_below, agg.f_hot,
               agg.p_sum, agg.p_n, agg.p_max, agg.p_in_band, agg.p_sensors,
               agg.p_below, agg.p_hot,
               agg.n_sum, agg.n_n, agg.n_max, agg.n_in_band, agg.n_sensors,
               agg.n_below, agg.n_hot,
               -- The far end of the rate, per source, so the comparison is
               -- made between two readings of the SAME sensors.
               was.pc_sum, COALESCE(was.pc_n, 0) AS pc_n,
               COALESCE(was.pc_sensors, 0) AS pc_sensors, was.pc_max,
               was.nc_sum, COALESCE(was.nc_n, 0) AS nc_n,
               COALESCE(was.nc_sensors, 0) AS nc_sensors, was.nc_max,
               agg.e_sum, agg.e_n, agg.e_sensors,
               was.c_sum, COALESCE(was.c_n, 0) AS c_n,
               COALESCE(was.c_sensors, 0) AS c_sensors, was.c_max,
               agg.rh_sum, agg.rh_n, agg.rh_max, agg.rh_probes
        FROM rack r
        JOIN rack_row rr   ON rr.id = r.row_id
        JOIN room rm       ON rm.id = rr.room_id
        JOIN datacenter dc ON dc.id = rm.datacenter_id
        LEFT JOIN agg      ON agg.rack_id = r.id
        LEFT JOIN was      ON was.rack_id = r.id
        ORDER BY dc.code, rm.name,
                 COALESCE(agg.p_max, agg.f_max, agg.n_max) DESC NULLS LAST,
                 rr.ordinal, r.ordinal, r.name
    """), {"t0": since, "was": was_at, "was_floor": was_at - NOW_HORIZON,
           "low": low_c, "high": high_c,
           "allow": allowable_c})).mappings().all()
    return [dict(r) for r in rows]


async def thermal_p90_now(session: AsyncSession, *,
                          since: datetime, force: str = "") -> dict[str, Any]:
    """The 90th percentile across SENSORS at this instant, per tier.

    Both forms rank sensors: the windowed one collapses each to its mean over
    the window first, this one has a single reading per sensor to begin with,
    which is what "the ninetieth percentile rack right now" means.
    """
    rows = (await session.execute(text("""
        WITH latest AS (
            SELECT DISTINCT ON (t.device_id, t.metric_id, t.instance)
                   t.device_id, t.value,
                   CASE WHEN m.key = 'component_temperature'
                        THEN 'network_intake' ELSE m.key END AS key
            FROM telemetry_sample t
            JOIN metric m ON m.id = t.metric_id
            JOIN device d ON d.id = t.device_id
                         AND d.rack_id IS NOT NULL
                         AND d.lifecycle <> 'decommissioned'
            LEFT JOIN device_type dt ON dt.code = d.device_type
            WHERE (m.key IN ('inlet_temperature', 'ambient_temperature')
                   OR (m.key = 'component_temperature'
                       AND t.instance = 'CHASSIS'
                       AND dt.category = 'network'))
              AND t.ts >= :t0
            ORDER BY t.device_id, t.metric_id, t.instance, t.ts DESC
        ),
        s AS (
            SELECT d.rack_id, l.key, l.value
            FROM latest l JOIN device d ON d.id = l.device_id
        ),
        -- One source per rack, in the order the rows above choose in: probe,
        -- then server, then the switches' front panel. Pooling two of them
        -- would take a percentile over readings measured in different places.
        src AS (
            SELECT rack_id,
                   bool_or(key = 'ambient_temperature') AS has_probe,
                   bool_or(key = 'inlet_temperature')   AS has_server
            FROM s GROUP BY rack_id
        ),
        chosen AS (
            SELECT s.rack_id, s.value
            FROM s JOIN src USING (rack_id)
            -- `force` pins every rack to one source, for a reader comparing
            -- what the probes say against what the servers say. Honoured HERE
            -- as well as in the rows: a percentile taken over a different
            -- population from the average beside it is two answers to one
            -- question.
            WHERE s.key = CASE
                      WHEN :force <> '' THEN :force
                      WHEN src.has_probe  THEN 'ambient_temperature'
                      WHEN src.has_server THEN 'inlet_temperature'
                      ELSE 'network_intake' END
        ),
        located AS (
            SELECT c.rack_id, rr.room_id, rm.datacenter_id, c.value
            FROM chosen c
            JOIN rack r      ON r.id  = c.rack_id
            JOIN rack_row rr ON rr.id = r.row_id
            JOIN room rm     ON rm.id = rr.room_id
        )
        SELECT rack_id::text       AS rack_id,
               room_id::text       AS room_id,
               datacenter_id::text AS datacenter_id,
               -- A percentile needs a population to rank. One sensor has no
               -- ninetieth of anything: taken over it, p90 is that sensor's
               -- own mean, which is the average printed beside it - and the
               -- two are rounded down different paths, so the column could
               -- show a p90 a tenth BELOW the average it was repeating.
               -- Most racks hold exactly one probe, so this is the common
               -- case at the rack tier, not an edge of it.
               CASE WHEN count(*) > 1
                    THEN percentile_cont(0.9) WITHIN GROUP (ORDER BY value)
               END AS p90
        FROM located
        GROUP BY GROUPING SETS ((rack_id), (room_id), (datacenter_id), ())
    """), {"t0": since, "force": force})).mappings().all()
    out: dict[str, Any] = {"racks": {}, "rooms": {}, "sites": {}, "total": None}
    for r in rows:
        p90 = None if r["p90"] is None else float(r["p90"])
        if r["rack_id"] is not None:
            out["racks"][r["rack_id"]] = p90
        elif r["room_id"] is not None:
            out["rooms"][r["room_id"]] = p90
        elif r["datacenter_id"] is not None:
            out["sites"][r["datacenter_id"]] = p90
        else:
            out["total"] = p90
    return out


async def thermal_alarms(session: AsyncSession, *,
                         categories: list[str]) -> dict[str, Any]:
    """Open thermal conditions per rack, room, site and estate.

    Counted by CATEGORY, and by the same categories the drill-down opens
    with, so the number on a row and the rows behind it can never disagree -
    a count an operator cannot reconcile with what a click shows is worse
    than no count.

    A condition is placed where its DEVICE is: a probe or a server in a rack
    counts against that rack, a CRAH or an air handler standing on the floor
    counts against its room. Both roll up to the site, so a room's figure
    includes what its racks are raising and what nothing in a rack is.

    Returns {"devices": {id: n}, "racks": {...}, "rooms": {...},
    "sites": {...}, "total": n}. The device level is what lets a cooling
    unit say whether anybody has been told about it, using the same
    predicate and the same categories as the rows above it - so the units
    in a hall add up to the hall's figure instead of meaning something
    slightly different.
    """
    rows = (await session.execute(text("""
        WITH located AS (
            SELECT a.id,
                   d.id AS device_id,
                   d.rack_id,
                   COALESCE(rr.room_id, d.room_id) AS room_id,
                   COALESCE(rm.datacenter_id, rm2.datacenter_id) AS datacenter_id
            FROM alarm a
            JOIN device d      ON d.id = a.device_id
            LEFT JOIN rack r   ON r.id = d.rack_id
            LEFT JOIN rack_row rr ON rr.id = r.row_id
            LEFT JOIN room rm  ON rm.id = rr.room_id
            LEFT JOIN room rm2 ON rm2.id = d.room_id
            -- The same population the drill-down shows, to the predicate:
            -- open is "not cleared", not "active", and a symptom is a
            -- consequence of a root condition rather than a second thing to
            -- answer. Counting either differently gives a number that does
            -- not survive being clicked.
            WHERE a.state <> 'CLEARED'
              AND a.shelved_reason IS NULL
              AND a.is_symptom = false
              AND a.category = ANY(:cats)
              AND d.lifecycle <> 'decommissioned'
        )
        SELECT device_id::text     AS device_id,
               rack_id::text       AS rack_id,
               room_id::text       AS room_id,
               datacenter_id::text AS datacenter_id,
               count(*)            AS n
        FROM located
        GROUP BY GROUPING SETS ((device_id), (rack_id), (room_id),
                                (datacenter_id), ())
    """), {"cats": list(categories)})).mappings().all()
    out: dict[str, Any] = {"devices": {}, "racks": {}, "rooms": {},
                           "sites": {}, "total": 0}
    for r in rows:
        n = int(r["n"] or 0)
        if r["device_id"] is not None:
            out["devices"][r["device_id"]] = n
        elif r["rack_id"] is not None:
            out["racks"][r["rack_id"]] = n
        elif r["room_id"] is not None:
            out["rooms"][r["room_id"]] = n
        elif r["datacenter_id"] is not None:
            out["sites"][r["datacenter_id"]] = n
        else:
            out["total"] = n
    return out


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


LIFECYCLES = ("open", "all")


def _state_clause(lifecycle: str) -> str:
    """The rows a lifecycle admits.

    `open` is what every counter counts; `all` is the same population with
    the closed rows back in it - the history behind a room, not its present.
    Anything else is a caller mistake, and a silent fallback to `open` would
    make a history panel quietly show the present instead.
    """
    if lifecycle not in LIFECYCLES:
        raise ValueError(f"unknown lifecycle: {lifecycle}")
    return "a.state <> 'CLEARED' AND " if lifecycle == "open" else ""


def _facet_clauses(severities: list[str] | None,
                   detections: list[str] | None,
                   params: dict[str, Any]) -> str:
    """The facet filters, as SQL on the alarm row, binding their lists.

    Empty or absent means "no filter", never "match nothing": a chip row
    with nothing pressed shows everything, which is what the counts on the
    chips describe.
    """
    sql = ""
    if severities:
        sql += " AND a.severity::text = ANY(:severities)"
        params["severities"] = [s.upper() for s in severities]
    if detections:
        sql += " AND a.detection = ANY(:detections)"
        params["detections"] = detections
    return sql


async def alarms_by_room(session: AsyncSession, *,
                         categories: list[str],
                         lifecycle: str = "open",
                         severities: list[str] | None = None,
                         detections: list[str] | None = None) -> list[dict[str, Any]]:
    """Root alarms of one category, grouped by room - with the alerts beside them.

    `lifecycle` picks the population: `open` (the default, and what every
    counter totals) or `all`, which puts the cleared rows back so a room's
    row says what it has raised rather than what it is raising.

    `severities` and `detections` narrow it further - the facet chips
    pressed on the panel. Applied here, on the row, so every figure that
    comes back (the room counts, the device counts, the per-row splits)
    describes the same narrowed population and still adds up.

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

    params: dict[str, Any] = {"categories": categories}
    rows = (await session.execute(text(f"""
        WITH {_DEV_CTE},
        cat AS (
            SELECT dev.datacenter_id, dev.room_id, a.severity::text AS severity,
                   a.device_id, a.detection AS detection,
                   a.category AS category, a.response_class AS response_class
            FROM alarm a
            JOIN dev ON dev.device_id = a.device_id
            WHERE {_state_clause(lifecycle)}a.is_symptom = false
              AND a.shelved_reason IS NULL{_facet_clauses(severities, detections, params)}
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
    """), params)).mappings().all()
    return [dict(r) for r in rows]


TREND_BUCKETS = ("day", "week")


async def alarm_trend(session: AsyncSession, *,
                      categories: list[str], since: datetime,
                      until: datetime | None = None,
                      bucket: str = "day",
                      room_id: str | None = None,
                      datacenter_id: str | None = None) -> list[dict[str, Any]]:
    """Root conditions raised per UTC day (or ISO week) since `since`, in a scope.

    `bucket` is `day` or `week`; a week is Postgres's `date_trunc('week')`,
    Monday-anchored, and the row's `day` is the bucket's first day. Chosen
    by the caller from the window: a year of daily bars is 365 columns
    nobody can read, and a month of weekly ones is four.

    Counted in the database, not from a page of the alarm list. The list is
    capped at 500 rows and ordered by severity, so bucketing it in the browser
    counted the 500 most SEVERE conditions, not the most recent - at estate
    scope that was a chart of noise wearing the axis of a trend.

    Located conditions only, the same rule the room rows use: a platform
    condition belongs to no room and no site, and is reported beside the
    table rather than folded into it. A room or a site scope is inherently
    located; the estate scope stays consistent with them.

    Days with nothing raised are absent here; the service fills them, because
    a day that is missing and a day with zero are the same fact to a chart.
    """
    if bucket not in TREND_BUCKETS:
        raise ValueError(f"unknown bucket: {bucket}")
    where = ["a.is_symptom = false",
             "a.shelved_reason IS NULL",
             "a.category = ANY(:categories)",
             "a.first_seen >= :since",
             "dev.room_id IS NOT NULL"]
    params: dict[str, Any] = {"categories": categories, "since": since,
                              "bucket": bucket}
    # `until` is EXCLUSIVE: the service passes the instant after the last
    # day of the window, so a window ending on the 7th holds all of the 7th.
    if until is not None:
        where.append("a.first_seen < :until")
        params["until"] = until
    if room_id:
        where.append("dev.room_id = CAST(:room_id AS uuid)")
        params["room_id"] = room_id
    if datacenter_id:
        where.append("dev.datacenter_id = CAST(:datacenter_id AS uuid)")
        params["datacenter_id"] = datacenter_id
    rows = (await session.execute(text(f"""
        WITH {_DEV_CTE}
        SELECT date_trunc(:bucket, a.first_seen AT TIME ZONE 'UTC')::date AS day,
               count(*)                                                   AS n
        FROM alarm a
        JOIN dev ON dev.device_id = a.device_id
        WHERE {" AND ".join(where)}
        GROUP BY 1
        ORDER BY 1
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def unlocated_alarms_by_category(session: AsyncSession, *,
                                       categories: list[str],
                                       lifecycle: str = "open",
                                       severities: list[str] | None = None,
                                       detections: list[str] | None = None) -> dict[str, int]:
    """Alarms of a category that resolve to no room.

    Platform conditions hang off devices with no location. They are counted in
    the strip, so the drill-down has to account for them or the modal will
    appear to have lost rows. Both classes, matching the counter.
    """
    params: dict[str, Any] = {"categories": categories}
    row = (await session.execute(text(f"""
        WITH {_DEV_CTE},
        cat AS (
            SELECT dev.room_id, a.category AS category,
                   a.response_class AS response_class
            FROM alarm a
            LEFT JOIN dev ON dev.device_id = a.device_id
            WHERE {_state_clause(lifecycle)}a.is_symptom = false
              AND a.shelved_reason IS NULL{_facet_clauses(severities, detections, params)}
        )
        SELECT count(*)                                       AS n,
               count(*) FILTER (WHERE response_class = '{ALARM}') AS alarms
        FROM cat
        WHERE category = ANY(:categories) AND room_id IS NULL
    """), params)).mappings().first()
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

    The room-level answer to "is this data worth reading".

    Read from `endpoint_state`, which the ingest maintains, NOT by scanning
    `telemetry_sample`. The scan version aggregated a day of raw samples for
    every device in the room and took 15.9 s on a 145-device hall - the whole
    of the room drawer's load time - because there is no index that answers
    max(ts) per device cheaply: the primary key leads with device_id but then
    metric_id, so a per-device maximum still has to walk every metric.

    This is not an approximation of that scan. Telemetry reaches the platform
    only through an endpoint - the collector polls endpoints and writes what
    they return - so the newest `last_telemetry_at` across the room's endpoints
    IS the newest sample in the room, from the record the writer keeps as it
    writes. A device with no endpoint has no route by which a sample could
    arrive.
    """
    return (await session.execute(text(f"""
        WITH {_DEV_CTE}
        SELECT max(es.last_telemetry_at)
          FROM device_endpoint de
          JOIN dev ON dev.device_id = de.device_id
          JOIN endpoint_state es ON es.endpoint_id = de.id
         WHERE dev.room_id = CAST(:id AS uuid)
    """), {"id": room_id})).scalar()
