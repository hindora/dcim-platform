"""Energy deltas for PUE.

Counters, not gauges, so the arithmetic has to be counter arithmetic: sum the
positive increments between consecutive samples rather than taking last minus
first. The two agree on a well-behaved counter and disagree exactly when it
matters - a meter that rolls over or is replaced mid-window makes last-minus-
first understate the energy, or go negative, and a negative denominator would
turn a PUE into nonsense rather than into an obvious error.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Sum of positive increments per device, over a window, for one device type.
#
# The LAG is partitioned per device and instance: two meters' readings
# interleaved in time order would otherwise produce differences between
# unrelated counters.
_ENERGY_DELTA = text("""
    WITH stepped AS (
        SELECT d.id AS device_id, d.name,
               t.value - LAG(t.value) OVER (
                   PARTITION BY t.device_id, t.instance ORDER BY t.ts
               ) AS step
          FROM telemetry_sample t
          JOIN metric m ON m.id = t.metric_id
          JOIN device d ON d.id = t.device_id
          LEFT JOIN rack r      ON r.id = d.rack_id
          LEFT JOIN rack_row rr ON rr.id = r.row_id
          LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
         WHERE m.key = 'energy_consumed'
           AND d.device_type = ANY(:types)
           AND d.lifecycle <> 'decommissioned'
           AND t.ts >= :start AND t.ts < :end
           -- Device-level totals only. The branch-circuit instances on an
           -- energy monitor are a breakdown OF that total, and adding them to
           -- it would count the same kilowatt-hours twice.
           AND t.instance = ''
           AND (CAST(:dc_id AS uuid) IS NULL
                OR rm.datacenter_id = CAST(:dc_id AS uuid))
    )
    SELECT device_id::text AS device_id, name,
           -- Only positive steps: a negative one is a reset or a replaced
           -- meter, and the energy before it was already counted.
           COALESCE(sum(step) FILTER (WHERE step > 0), 0) AS kwh,
           count(*) FILTER (WHERE step IS NOT NULL)       AS steps,
           count(*) FILTER (WHERE step < 0)               AS resets
      FROM stepped
     GROUP BY device_id, name
""")

# Instantaneous power, for the fallback when energy counters are unusable.
_POWER_NOW = text("""
    SELECT d.id::text AS device_id, d.name, ds.power_w
      FROM device d
      JOIN device_state ds ON ds.device_id = d.id
      LEFT JOIN rack r      ON r.id = d.rack_id
      LEFT JOIN rack_row rr ON rr.id = r.row_id
      LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
     WHERE d.device_type = ANY(:types)
       AND d.lifecycle <> 'decommissioned'
       AND ds.power_w IS NOT NULL
       AND (CAST(:dc_id AS uuid) IS NULL
                OR rm.datacenter_id = CAST(:dc_id AS uuid))
""")


async def energy_delta(session: AsyncSession, *, device_types: list[str],
                       start: datetime, end: datetime,
                       datacenter_id: str | None = None) -> dict[str, Any]:
    rows = (await session.execute(_ENERGY_DELTA, {
        "types": device_types, "start": start, "end": end,
        "dc_id": datacenter_id,
    })).mappings().all()
    return {
        "kwh": float(sum(r["kwh"] for r in rows)),
        "devices": [dict(r) for r in rows],
        "resets": sum(r["resets"] for r in rows),
        "steps": sum(r["steps"] for r in rows),
    }


async def power_now(session: AsyncSession, *, device_types: list[str],
                    datacenter_id: str | None = None) -> dict[str, Any]:
    rows = (await session.execute(_POWER_NOW, {
        "types": device_types, "dc_id": datacenter_id,
    })).mappings().all()
    return {
        "watts": float(sum(r["power_w"] for r in rows)),
        "devices": [dict(r) for r in rows],
    }
