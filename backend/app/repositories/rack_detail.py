"""What one rack's devices read right now, for the rack page's thermal block.

The hot mirror (`device_state.metrics`) keeps the newest value per metric key,
so this answers "what is the air doing in this rack" without the hypertable.
The keys are the ones the Thermal page's rack query pools: a server's own
inlet and exhaust, a switch's component temperature standing in for an
intake it does not measure, and the ambient reading of a probe or a strip's
built-in head in the cold aisle.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def thermal_points(session: AsyncSession, rack_id: str) -> list[dict[str, Any]]:
    """Every device standing in the rack with the temperatures it reports,
    top of the rack first, and the room's ASHRAE class on each row."""
    rows = (await session.execute(text("""
        SELECT d.id::text, d.name, d.device_type::text AS device_type, dt.category,
               d.u_start, d.u_height,
               (ds.metrics->'inlet_temperature'->>'v')::float8     AS inlet_c,
               (ds.metrics->'exhaust_temperature'->>'v')::float8   AS exhaust_c,
               (ds.metrics->'ambient_temperature'->>'v')::float8   AS ambient_c,
               (ds.metrics->'component_temperature'->>'v')::float8 AS component_c,
               (ds.metrics->'cpu_temperature'->>'v')::float8       AS cpu_c,
               (ds.metrics->'relative_humidity'->>'v')::float8     AS rh_pct,
               COALESCE(ds.metrics->'inlet_temperature'->>'t',
                        ds.metrics->'ambient_temperature'->>'t',
                        ds.metrics->'component_temperature'->>'t') AS read_at,
               rm.ashrae_class
          FROM device d
          JOIN device_type dt       ON dt.code = d.device_type
          LEFT JOIN device_state ds ON ds.device_id = d.id
          LEFT JOIN rack r          ON r.id = d.rack_id
          LEFT JOIN rack_row rr     ON rr.id = r.row_id
          LEFT JOIN room rm         ON rm.id = rr.room_id
         WHERE d.rack_id = CAST(:rack AS uuid)
           AND d.lifecycle <> 'decommissioned'
         ORDER BY d.u_start DESC NULLS LAST, d.name
    """), {"rack": rack_id})).mappings().all()
    return [dict(r) for r in rows]
