"""Thermal readings per rack and per CRAH, over a sustained window."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
           max(v.setpoint) AS setpoint_c
      FROM device d
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
              ORDER BY t.ts DESC LIMIT 1) AS setpoint
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
           AND tb.ts > now() - interval '30 minutes'
         ORDER BY tb.device_id, tb.ts DESC
    """), {"room_id": room_id})).mappings().all()
    return {r["device_id"]: bool(r["value"]) for r in rows}
