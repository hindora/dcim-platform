"""Thermal readings per rack and per CRAH, over a sustained window."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.changelog import LAST_KNOWN_WINDOW_S

# Per-rack intake and exhaust over the window.
#
# min(inlet) is carried as well as the mean because "sustained" has to be
# tested against the minimum: a rack whose coldest reading in fifteen minutes
# is still above the threshold was hot for the whole window, where a mean can
# be dragged over it by one spike.
_RACK = text("""
    SELECT r.id::text AS rack_id, r.name,
           avg(t.value) FILTER (WHERE m.key = 'inlet_temperature')   AS inlet_mean,
           min(t.value) FILTER (WHERE m.key = 'inlet_temperature')   AS inlet_min,
           max(t.value) FILTER (WHERE m.key = 'inlet_temperature')   AS inlet_max,
           avg(t.value) FILTER (WHERE m.key = 'exhaust_temperature') AS exhaust_mean,
           count(*)     FILTER (WHERE m.key = 'inlet_temperature')   AS samples
      FROM telemetry_sample t
      JOIN metric m  ON m.id = t.metric_id
      JOIN device d  ON d.id = t.device_id
      JOIN rack r    ON r.id = d.rack_id
      JOIN rack_row rr ON rr.id = r.row_id
     WHERE m.key IN ('inlet_temperature', 'exhaust_temperature')
       AND t.ts > now() - make_interval(mins => :minutes)
       AND d.lifecycle <> 'decommissioned'
       AND rr.room_id = CAST(:room_id AS uuid)
     GROUP BY r.id, r.name
""")

# Latest air temperatures per CRAH. Latest rather than averaged: the question
# "is this unit failing right now" is about now, and a mean over fifteen
# minutes hides a unit that failed twelve minutes ago.
_CRAH = text("""
    SELECT d.id::text AS device_id, d.name,
           max(v.supply)   AS supply_c,
           max(v.ret)      AS return_c,
           max(v.setpoint) AS setpoint_c,
           max(v.valve)    AS valve_pct,
           max(v.fan)      AS fan_pct,
           max(v.duty)     AS duty_pct,
           max(md.rated_cooling_w) AS rated_cooling_w
      FROM device d
      -- The datasheet figure, which lives on the SKU: every PCW 100kW removes
      -- 100 kW. NULL for a model the platform has no rating for, and the page
      -- then shows the share rather than inventing kilowatts.
      LEFT JOIN model md    ON md.id = d.model_id
      LEFT JOIN rack r      ON r.id = d.rack_id
      LEFT JOIN rack_row rr ON rr.id = r.row_id
      LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
      JOIN LATERAL (
          SELECT
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'supply_air_temp'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS supply,
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'return_air_temp'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS ret,
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'air_setpoint_temp'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS setpoint,
            -- The two that say whether a warm discharge is this machine's
            -- fault or the plant's. A valve pinned open with the air still
            -- warm is water that is not cold enough or not arriving; a valve
            -- modulating gently with the same warm air is the unit itself.
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'valve_position_pct'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS valve,
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'fan_speed_pct'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS fan,
            -- Heat actually being carried away, as a share of what this unit
            -- is rated for. Not derivable from the columns beside it: air-side
            -- duty is mass flow times delta-T, so a unit at a wide delta and
            -- low airflow can be doing the same work as one at the opposite.
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'cooling_output_pct'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS duty
      ) v ON TRUE
     WHERE d.device_type = 'crah'
       AND d.lifecycle <> 'decommissioned'
       AND rm.id = CAST(:room_id AS uuid)
     GROUP BY d.id, d.name
""")


#: The LIQUID half of a hall's cooling. A CDU is a heat exchanger between the
#: facility chilled water and the technology-cooling (cold-plate) loop that
#: feeds direct-to-chip servers, and it stands in the hall exactly as a CRAH
#: does - in a row, keyed on the same room.
#:
#: Every temperature here is the SECONDARY loop, which is the one an operator
#: is asking about: it is what the plates are actually being fed. The facility
#: side appears only as the control valve, because the valve running out of
#: travel is how a CDU says the primary is not keeping up with it.
#:
#: Instances matter on every point. A CDU publishes `water_flow` twice, once
#: for each loop, and `valve_position_pct` only for the facility side - reading
#: either without its instance mixes the two loops into one column.
_CDU = text("""
    SELECT d.id::text AS device_id, d.name,
           max(v.supply)   AS supply_c,
           max(v.ret)      AS return_c,
           max(v.setpoint) AS setpoint_c,
           max(v.flow)     AS flow_l_s,
           max(v.valve)    AS valve_pct,
           max(v.pump)     AS pump_pct,
           max(v.heat)     AS heat_w,
           max(v.approach) AS approach_k,
           max(v.dp)       AS filter_dp_kpa,
           max(md.rated_cooling_w) AS rated_cooling_w
      FROM device d
      LEFT JOIN model md    ON md.id = d.model_id
      LEFT JOIN rack r      ON r.id = d.rack_id
      LEFT JOIN rack_row rr ON rr.id = r.row_id
      LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
      JOIN LATERAL (
          SELECT
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'water_supply_temp'
                AND t.instance = 'TCS'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS supply,
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'water_return_temp'
                AND t.instance = 'TCS'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS ret,
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'water_setpoint_temp'
                AND t.instance = 'TCS'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS setpoint,
            -- Secondary flow. With the range beside it this is the check on
            -- the heat in the next column: Q = flow x range x cp, and a unit
            -- whose own three numbers do not multiply out is not to be read.
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'water_flow'
                AND t.instance = 'TCS'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS flow,
            -- FACILITY side: the valve the unit modulates to hold its
            -- secondary setpoint. Pinned open with the coolant still warm is
            -- the primary loop failing it, not the exchanger.
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'valve_position_pct'
                AND t.instance = 'FACILITY_CHW'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS valve,
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'pump_speed_pct'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS pump,
            -- The heat the plates are handing it, in watts, published by the
            -- machine rather than inferred from the loop beside it.
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'thermal_load'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS heat,
            -- How close the secondary gets to the facility water. It widens as
            -- the exchanger fouls, which nothing else on the row can show.
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'approach_temp'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS approach,
            (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
              WHERE t.device_id = d.id AND m.key = 'filter_diff_pressure'
                AND t.ts > now() - interval '30 minutes'
              ORDER BY t.ts DESC LIMIT 1) AS dp
      ) v ON TRUE
     WHERE d.device_type = 'cdu'
       AND d.lifecycle <> 'decommissioned'
       AND rm.id = CAST(:room_id AS uuid)
     GROUP BY d.id, d.name
""")


#: Cooling units across the estate, latest reading each, with the room they
#: stand in.
#:
#: The room view asks this one room at a time, because that is what it is. The
#: estate table needs every hall at once, so the filter goes and a room id
#: comes back instead - one scan of about thirty machines rather than a query
#: per row.
#:
#: Run status is the last-known BOOLEAN, not a temperature: booleans are stored
#: on change plus a heartbeat, so "the newest row inside the window" is the
#: only way to read one, and a unit that stopped an hour ago has no fresher row
#: saying so.
_ESTATE_CRAH = text("""
    SELECT d.id::text  AS device_id,
           d.name      AS name,
           rm.id::text AS room_id,
           (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
             WHERE t.device_id = d.id AND m.key = 'supply_air_temp'
               AND t.ts > now() - interval '30 minutes'
             ORDER BY t.ts DESC LIMIT 1) AS supply_c,
           (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
             WHERE t.device_id = d.id AND m.key = 'return_air_temp'
               AND t.ts > now() - interval '30 minutes'
             ORDER BY t.ts DESC LIMIT 1) AS return_c,
           (SELECT t.value FROM telemetry_sample t JOIN metric m ON m.id = t.metric_id
             WHERE t.device_id = d.id AND m.key = 'air_setpoint_temp'
               AND t.ts > now() - interval '30 minutes'
             ORDER BY t.ts DESC LIMIT 1) AS setpoint_c,
           (SELECT tb.value FROM telemetry_bool tb JOIN metric m ON m.id = tb.metric_id
             WHERE tb.device_id = d.id AND m.key = 'equipment_state'
               AND tb.ts > now() - make_interval(secs => :window_s)
             ORDER BY tb.ts DESC LIMIT 1) AS running
      FROM device d
      LEFT JOIN rack r      ON r.id = d.rack_id
      LEFT JOIN rack_row rr ON rr.id = r.row_id
      LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
     WHERE d.device_type = 'crah'
       AND d.lifecycle <> 'decommissioned'
       AND rm.id IS NOT NULL
""")


async def crahs_by_room(session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    """Every cooling unit in the estate, grouped by the room it stands in."""
    rows = (await session.execute(
        _ESTATE_CRAH, {"window_s": LAST_KNOWN_WINDOW_S})).mappings().all()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["room_id"], []).append(dict(r))
    return out


async def racks(session: AsyncSession, *, room_id: str,
                minutes: int) -> list[dict[str, Any]]:
    rows = (await session.execute(_RACK, {"room_id": room_id,
                                          "minutes": minutes})).mappings().all()
    return [dict(r) for r in rows]


async def crahs(session: AsyncSession, *, room_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(_CRAH, {"room_id": room_id})).mappings().all()
    return [dict(r) for r in rows]


async def cdus(session: AsyncSession, *, room_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(_CDU, {"room_id": room_id})).mappings().all()
    return [dict(r) for r in rows]


async def running_cdus(session: AsyncSession, room_id: str) -> dict[str, bool]:
    """Same last-known read as the CRAHs, on the same boolean."""
    rows = (await session.execute(text("""
        SELECT DISTINCT ON (tb.device_id) tb.device_id::text AS device_id, tb.value
          FROM telemetry_bool tb
          JOIN metric m ON m.id = tb.metric_id
          JOIN device d ON d.id = tb.device_id
          LEFT JOIN rack r      ON r.id = d.rack_id
          LEFT JOIN rack_row rr ON rr.id = r.row_id
          LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
         WHERE m.key = 'equipment_state'
           AND d.device_type = 'cdu'
           AND rm.id = CAST(:room_id AS uuid)
           AND tb.ts > now() - make_interval(secs => :window_s)
         ORDER BY tb.device_id, tb.ts DESC
    """), {"room_id": room_id,
           "window_s": LAST_KNOWN_WINDOW_S})).mappings().all()
    return {r["device_id"]: bool(r["value"]) for r in rows}


async def running_crahs(session: AsyncSession, room_id: str) -> dict[str, bool]:
    rows = (await session.execute(text("""
        SELECT DISTINCT ON (tb.device_id) tb.device_id::text AS device_id, tb.value
          FROM telemetry_bool tb
          JOIN metric m ON m.id = tb.metric_id
          JOIN device d ON d.id = tb.device_id
          LEFT JOIN rack r      ON r.id = d.rack_id
          LEFT JOIN rack_row rr ON rr.id = r.row_id
          LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
         WHERE m.key = 'equipment_state'
           AND d.device_type = 'crah'
           AND rm.id = CAST(:room_id AS uuid)
           -- last-known window: booleans are stored on change plus a
           -- heartbeat (app.ingest.changelog), not every poll.
           AND tb.ts > now() - make_interval(secs => :window_s)
         ORDER BY tb.device_id, tb.ts DESC
    """), {"room_id": room_id,
           "window_s": LAST_KNOWN_WINDOW_S})).mappings().all()
    return {r["device_id"]: bool(r["value"]) for r in rows}
