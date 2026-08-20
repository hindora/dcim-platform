"""Latest cooling telemetry, per device and per loop."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Metrics the plant view needs. Instance matters: a chiller carries two water
# loops - CHW on the evaporator and COND on the condenser - and averaging their
# temperatures together would produce a number describing nothing.
_KEYS = (
    "water_supply_temp", "water_return_temp", "water_flow", "water_setpoint_temp",
    "cooling_capacity", "cop", "power_draw", "compressor_load_pct",
    "thermal_load", "supply_air_temp", "return_air_temp", "cooling_output_pct",
    "run_hours",
)

# One row per (device, metric, instance): the newest sample inside the window.
#
# DISTINCT ON rather than a window function because it is the cheapest way to
# say "latest per group" in Postgres, and the window is bounded so a device that
# stopped reporting an hour ago does not present a stale reading as current.
_LATEST = text("""
    SELECT DISTINCT ON (t.device_id, m.key, t.instance)
           t.device_id::text AS device_id, d.name, d.device_type::text AS device_type,
           COALESCE(ds.status::text, 'UNKNOWN') AS status,
           rm.id::text AS room_id, rm.name AS room_name,
           m.key, t.instance, t.value, t.ts
      FROM telemetry_sample t
      JOIN metric m  ON m.id = t.metric_id
      JOIN device d  ON d.id = t.device_id
      LEFT JOIN device_state ds ON ds.device_id = d.id
      LEFT JOIN rack r      ON r.id = d.rack_id
      LEFT JOIN rack_row rr ON rr.id = r.row_id
      LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
     WHERE t.ts > now() - interval '10 minutes'
       AND m.key = ANY(:keys)
       AND d.device_type = ANY(:types)
       AND d.lifecycle <> 'decommissioned'
     ORDER BY t.device_id, m.key, t.instance, t.ts DESC
""")

PLANT_TYPES = ["chiller", "cooling_tower", "pump", "crah", "cdu"]


async def latest(session: AsyncSession,
                 device_types: list[str] | None = None) -> list[dict[str, Any]]:
    rows = (await session.execute(_LATEST, {
        "keys": list(_KEYS),
        "types": device_types or PLANT_TYPES,
    })).mappings().all()
    return [dict(r) for r in rows]


async def machine_flags(session: AsyncSession) -> dict[str, dict[str, bool]]:
    """Running and alarm state per plant machine.

    Read from telemetry_bool, not inferred from power draw. A chiller staged off
    by the BMS is a healthy decision and a chiller that has tripped is not, but
    both draw almost nothing - so power alone cannot tell them apart, and
    staging analysis that guessed from watts would call a trip "standby".
    """
    rows = (await session.execute(text("""
        SELECT DISTINCT ON (t.device_id, m.key)
               t.device_id::text AS device_id, m.key, t.value
          FROM telemetry_bool t
          JOIN metric m ON m.id = t.metric_id
         WHERE t.ts > now() - interval '10 minutes'
           AND m.key IN ('equipment_state', 'alarm_state')
         ORDER BY t.device_id, m.key, t.ts DESC
    """))).mappings().all()
    out: dict[str, dict[str, bool]] = {}
    for r in rows:
        out.setdefault(r["device_id"], {})[r["key"]] = bool(r["value"])
    return out


async def nameplate_kw(session: AsyncSession) -> dict[str, float]:
    """Rated cooling capacity per machine, as the highest value ever observed.

    The obvious source would be inventory, but model.rated_capacity is null on
    this fleet - the rating lives only in the model NAME ("Carrier 19DV 800kW"),
    and parsing capacity out of a marketing string is not a foundation to put
    capacity planning on.

    So: the reported cooling_capacity point, which reads the machine's rating
    while it runs and drops to zero when it stops. A stopped chiller's nameplate
    has not changed, so the highest value seen over a day is its rating. The
    cost is that a machine which has not run all day has no nameplate here, and
    the caller is told rather than shown a confident zero.
    """
    rows = (await session.execute(text("""
        SELECT d.id::text AS device_id, max(t.value) AS rated_w
          FROM telemetry_sample t
          JOIN metric m ON m.id = t.metric_id
          JOIN device d ON d.id = t.device_id
         WHERE m.key = 'cooling_capacity'
           AND t.ts > now() - interval '24 hours'
         GROUP BY d.id
        HAVING max(t.value) > 0
    """))).mappings().all()
    return {r["device_id"]: float(r["rated_w"]) / 1000.0 for r in rows}
