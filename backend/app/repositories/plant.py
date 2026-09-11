"""The cooling plant as inventory plus its newest reading, machine by machine.

Separate from repositories/cooling.py, which answers a narrower question: that
one assembles chillers and loops for the analytics drawer and reads only the
keys a chiller needs. The estate plant table walks the whole chain - air
handlers, coolant distribution units, pumps, valves, chillers, towers - so it
needs every machine of every cooling type, the site it stands in, and its
nameplate, including for a machine that has been staged off all day and is
publishing zeroes.

Three deliberate choices:

  * Nameplate comes from the MODEL, not from the live capacity point. A stopped
    chiller reports zero kW of capacity, and a plant view that reads redundancy
    off the live point erases every standby machine exactly when somebody is
    asking whether standby exists. The observed point is kept as a fallback for
    a model with no rating on file.

  * Run state comes from telemetry_bool, never from power draw. A chiller
    staged off by the BMS and a chiller that has tripped both draw almost
    nothing; only the BACnet binary says which.

  * Alarms come from the alarm table, the same rows the alert pages open. A
    machine's alarm count here and the drill-down behind it are the same
    predicate, so they cannot disagree.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingest.changelog import LAST_KNOWN_WINDOW_S

#: Every device type that moves heat, in no particular order - the service
#: decides what order the chain reads in.
PLANT_TYPES = ["crah", "cdu", "pump", "valve", "chiller", "cooling_tower"]

#: The points the plant table reads. Instance matters throughout: a chiller
#: carries CHW on the evaporator and COND on the condenser, a pump reports
#: SUCTION and DISCHARGE pressure, a valve reports COMMANDED and MEASURED
#: position - and averaging any of those pairs produces a number describing
#: nothing. They are kept apart all the way to the row.
_KEYS = (
    # Water loops
    "water_supply_temp", "water_return_temp", "water_flow", "water_setpoint_temp",
    "water_pressure", "water_diff_pressure",
    # Air side
    "supply_air_temp", "return_air_temp", "air_setpoint_temp", "airflow",
    "cooling_output_pct",
    # Machine effort
    "fan_speed_pct", "pump_speed_pct", "valve_position_pct", "vfd_frequency",
    "compressor_load_pct",
    # Energy and output
    "power_draw", "cop", "cooling_capacity", "thermal_load",
    # Condition
    "approach_temp", "filter_diff_pressure", "basin_level_pct", "vibration",
    "motor_temp", "run_hours",
    # Weather, which is what a tower's performance has to be judged against
    "outdoor_wet_bulb_temp", "outdoor_dry_bulb_temp",
)

#: Inventory: every cooling machine, where it stands, and what it is rated for.
#:
#: LEFT JOINs the whole way down because a plant machine is floor-standing -
#: it has a room and no rack - and an inner join on rack would silently drop
#: the entire chiller plant.
_MACHINES = text("""
    SELECT d.id::text        AS device_id,
           d.name            AS name,
           d.device_type::text AS device_type,
           COALESCE(ds.status::text, 'UNKNOWN') AS status,
           rm.id::text       AS room_id,
           rm.name           AS room_name,
           dc.id::text       AS site_id,
           dc.code           AS site_code,
           dc.name           AS site_name,
           md.name           AS model_name,
           md.rated_cooling_w AS rated_cooling_w,
           md.rated_power_w   AS rated_power_w
      FROM device d
      LEFT JOIN device_state ds ON ds.device_id = d.id
      LEFT JOIN model md    ON md.id = d.model_id
      LEFT JOIN rack r      ON r.id = d.rack_id
      LEFT JOIN rack_row rr ON rr.id = r.row_id
      LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
      LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
     WHERE d.device_type = ANY(:types)
       AND d.lifecycle <> 'decommissioned'
     ORDER BY dc.code, d.name
""")

#: The newest sample per (device, key, instance) inside a short window.
#:
#: Bounded so a machine that stopped reporting half an hour ago does not
#: present a stale temperature as current: an empty row is a fact about the
#: machine, a stale one is a lie about the plant.
_LATEST = text("""
    SELECT DISTINCT ON (t.device_id, m.key, t.instance)
           t.device_id::text AS device_id, m.key, t.instance, t.value
      FROM telemetry_sample t
      JOIN metric m ON m.id = t.metric_id
      JOIN device d ON d.id = t.device_id
     WHERE t.ts > now() - interval '30 minutes'
       AND m.key = ANY(:keys)
       AND d.device_type = ANY(:types)
       AND d.lifecycle <> 'decommissioned'
     ORDER BY t.device_id, m.key, t.instance, t.ts DESC
""")

#: Run state per machine, and every alarm point it is holding true.
#:
#: Booleans are change-logged plus a heartbeat, so "newest row in the
#: last-known window" is the only correct way to read one - a machine that
#: stopped an hour ago has published nothing since saying so.
#:
#: Instance is kept: a CRAH carries four separate alarm points and collapsing
#: them to one boolean per device loses which condition is standing.
_FLAGS = text("""
    SELECT DISTINCT ON (t.device_id, m.key, t.instance)
           t.device_id::text AS device_id, m.key, t.instance, t.value
      FROM telemetry_bool t
      JOIN metric m ON m.id = t.metric_id
      JOIN device d ON d.id = t.device_id
     WHERE t.ts > now() - make_interval(secs => :window_s)
       AND m.key IN ('equipment_state', 'alarm_state')
       AND d.device_type = ANY(:types)
       AND d.lifecycle <> 'decommissioned'
     ORDER BY t.device_id, m.key, t.instance, t.ts DESC
""")

#: Open conditions per machine, by the predicate the alert pages use.
#:
#: Every category, not just the thermal ones: a chiller with a power alarm is
#: not a healthy chiller, and a plant table that counted only cooling
#: conditions would show it as quiet.
_ALARMS = text("""
    SELECT a.device_id::text AS device_id,
           count(*)          AS n,
           max(CASE a.severity
                 WHEN 'CRITICAL' THEN 3 WHEN 'MAJOR' THEN 2
                 WHEN 'MINOR' THEN 1 ELSE 0 END) AS worst
      FROM alarm a
      JOIN device d ON d.id = a.device_id
     WHERE a.state <> 'CLEARED'
       AND a.shelved_by_window IS NULL
       AND a.is_symptom = false
       AND d.device_type = ANY(:types)
       AND d.lifecycle <> 'decommissioned'
     GROUP BY a.device_id
""")

#: Rated capacity as observed, for a model with no figure on file.
#: The capacity point reads the machine's rating while it runs and zero when
#: it stops, so the highest value over a day is its nameplate.
_OBSERVED_KW = text("""
    SELECT t.device_id::text AS device_id, max(t.value) AS rated_w
      FROM telemetry_sample t
      JOIN metric m ON m.id = t.metric_id
      JOIN device d ON d.id = t.device_id
     WHERE m.key = 'cooling_capacity'
       AND t.ts > now() - interval '24 hours'
       AND d.device_type = ANY(:types)
     GROUP BY t.device_id
    HAVING max(t.value) > 0
""")


async def machines(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(
        _MACHINES, {"types": PLANT_TYPES})).mappings().all()
    return [dict(r) for r in rows]


async def latest(session: AsyncSession) -> dict[str, dict[tuple[str, str], float]]:
    """{device_id: {(key, instance): value}} - the newest of each."""
    rows = (await session.execute(
        _LATEST, {"keys": list(_KEYS), "types": PLANT_TYPES})).mappings().all()
    out: dict[str, dict[tuple[str, str], float]] = {}
    for r in rows:
        out.setdefault(r["device_id"], {})[
            (r["key"], r["instance"] or "")] = float(r["value"])
    return out


async def flags(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """{device_id: {"running": bool|None, "alarm_points": [instance, ...]}}."""
    rows = (await session.execute(
        _FLAGS, {"window_s": LAST_KNOWN_WINDOW_S,
                 "types": PLANT_TYPES})).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = out.setdefault(r["device_id"], {"running": None, "alarm_points": []})
        if r["key"] == "equipment_state":
            d["running"] = bool(r["value"])
        elif r["value"]:
            d["alarm_points"].append(r["instance"] or "alarm")
    return out


async def alarms(session: AsyncSession) -> dict[str, dict[str, int]]:
    rows = (await session.execute(
        _ALARMS, {"types": PLANT_TYPES})).mappings().all()
    return {r["device_id"]: {"open": int(r["n"]), "worst": int(r["worst"])}
            for r in rows}


async def observed_kw(session: AsyncSession) -> dict[str, float]:
    rows = (await session.execute(
        _OBSERVED_KW, {"types": PLANT_TYPES})).mappings().all()
    return {r["device_id"]: float(r["rated_w"]) / 1000.0 for r in rows}
