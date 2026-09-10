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


async def racks(session: AsyncSession, *, room_id: str,
                minutes: int) -> list[dict[str, Any]]:
    rows = (await session.execute(_RACK, {"room_id": room_id,
                                          "minutes": minutes})).mappings().all()
    return [dict(r) for r in rows]


async def crahs(session: AsyncSession, *, room_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(_CRAH, {"room_id": room_id})).mappings().all()
    return [dict(r) for r in rows]


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
