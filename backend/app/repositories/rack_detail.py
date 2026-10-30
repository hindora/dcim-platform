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


async def rack_p95(session: AsyncSession, rack_id: str, *, days: int = 7) -> float | None:
    """The 95th percentile of this rack's coincident hourly IT draw over the
    last `days`: the figure the Power page's headroom and stranded rest on,
    read for one rack.

    The estate read (`estate.power_p95`) sums every rack per bucket in one
    pass and the planner reads the whole week of hourly points to do it,
    which is the right trade for a page of racks and the wrong one for a
    page of one: the same arithmetic, scoped to the rack's devices before
    the rollup is touched. Loads with no reading of their own in the window
    are read at the strip outlet their cord lands on, as everywhere else.
    """
    row = (await session.execute(text("""
        WITH loads AS (
            SELECT d.id
              FROM device d
              JOIN device_type dt ON dt.code = d.device_type
             WHERE d.rack_id = CAST(:rack AS uuid)
               AND d.lifecycle <> 'decommissioned'
               AND dt.category IN ('it', 'network')
        ),
        self AS (
            SELECT t.bucket, t.device_id, t.avg_value AS w
              FROM telemetry_1h t
              JOIN metric m ON m.id = t.metric_id
             WHERE t.device_id IN (SELECT id FROM loads)
               AND m.key = 'power_draw' AND t.instance = ''
               AND t.bucket >= now() - make_interval(days => :days)
        ),
        self_devs AS (SELECT DISTINCT device_id FROM self),
        ol AS (
            SELECT t.bucket, t.device_id, t.instance, t.avg_value AS w
              FROM telemetry_1h t
              JOIN metric m ON m.id = t.metric_id
             WHERE t.device_id IN (SELECT id FROM device WHERE rack_id = CAST(:rack AS uuid)
                                     AND device_type::text = 'pdu')
               AND m.key = 'outlet_active_power'
               AND t.bucket >= now() - make_interval(days => :days)
        ),
        strip AS (
            SELECT x.bucket, c.b_device_id AS device_id, sum(x.w) AS w
              FROM ol x
              JOIN outlet o ON o.device_id = x.device_id
                           AND x.instance ~ '^[0-9]+$' AND o.number = x.instance::int
              JOIN connection c ON c.a_termination_type::text = 'outlet'
                               AND c.a_termination_id = o.id
                               AND c.layer = CAST('power' AS layer_t)
                               AND c.admin_state = 'enabled'
             WHERE c.b_device_id IN (SELECT id FROM loads)
               AND NOT EXISTS (SELECT 1 FROM self_devs s WHERE s.device_id = c.b_device_id)
             GROUP BY 1, 2
        ),
        per AS (
            SELECT bucket, sum(w) / 1000.0 AS kw
              FROM (SELECT bucket, w FROM self UNION ALL SELECT bucket, w FROM strip) b
             GROUP BY bucket
        )
        SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY kw) AS p95_kw FROM per
    """), {"rack": rack_id, "days": days})).mappings().first()
    v = row["p95_kw"] if row else None
    return None if v is None else round(float(v), 2)
