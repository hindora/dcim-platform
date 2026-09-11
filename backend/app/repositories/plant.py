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

#: Everything ELSE that stands in a facility room: the electrical spine, the
#: header instruments, the gateways the BMS talks through.
#:
#: These are not cooling stages and never appear in the chain. They are here
#: because a facility room is a room somebody walks into, and "what is in this
#: room and is it healthy" is a question the room table has to be able to
#: answer - the alternative, which is what the page did before, is a row of
#: dashes that drills into racks the room does not have.
FACILITY_TYPES = [
    "ups", "generator", "switchgear", "ats", "mcc", "mpp", "energy_monitor",
    "utility_feed", "sensor", "bacnet_router", "modbus_gateway", "pdu",
    "rpp", "oob_switch",
]

#: Both populations, for the queries that do not care which is which.
ALL_TYPES = PLANT_TYPES + FACILITY_TYPES

#: A facility type is only ever SHOWN where it stands in a room with no racks.
#: The estate has eighty power strips and thirty-six access switches, nearly
#: all of them in server halls, and this view never displays one of those - so
#: asking the telemetry tables about them costs a second of somebody's page
#: load to fetch rows that are then filtered away. The cooling types carry no
#: such filter: a CRAH lives in a hall by definition and the PLANT tab needs
#: every one of them.
_FACILITY_IN_FACILITY_ROOMS = """
    ( d.device_type = ANY(:cool_types)
      OR (d.device_type = ANY(:fac_types) AND rm.room_class = 'facility') )
"""

#: Which keys belong to which population. A chiller publishes no battery
#: health and a switchboard no chilled-water temperature, so asking for the
#: union against every device doubles the rows the index has to walk for
#: nothing.
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

#: Everything else in a facility room.
_FACILITY_KEYS = (
    # The electrical spine. `power_draw` means different things on different
    # machines - a chiller CONSUMES it, a utility feed CARRIES it - so the
    # service labels every one of these per type rather than summing them.
    "power_draw", "load_pct", "power_factor", "voltage_ll", "voltage_ln",
    "line_frequency", "phase_imbalance_pct", "voltage_thd_pct",
    "current_thd_pct", "demand_peak_power", "apparent_power",
    "energy_consumed",
    # UPS and generator condition, which is what a walk round the room checks.
    "battery_health_pct", "battery_runtime", "battery_temperature",
    "fuel_level_pct", "coolant_temperature", "current_run_time",
    "transfer_count", "time_on_emergency",
    # The room's own air, and the chassis that is the next best thing.
    "ambient_temperature", "relative_humidity", "component_temperature",
    # A header instrument on the same trunk reads water, not air.
    "water_supply_temp", "water_return_temp", "water_flow",
    # Evidence of life for a gateway or a router, which publishes nothing else
    # this view reads.
    "sys_uptime",
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
           rm.room_type::text  AS room_type,
           rm.room_class::text AS room_class,
           rm.floor          AS floor,
           dc.id::text       AS site_id,
           dc.code           AS site_code,
           dc.name           AS site_name,
           md.name           AS model_name,
           md.rated_cooling_w AS rated_cooling_w,
           md.rated_power_w   AS rated_power_w,
           -- Whether anything is even TRYING to read this device. A panel
           -- with no endpoint is inventory: it has never reported and never
           -- will, which is a different fact from a machine that has gone
           -- quiet, and the two must not share a word.
           (SELECT count(*) FROM device_endpoint de
             WHERE de.device_id = d.id) AS endpoints
      FROM device d
      LEFT JOIN device_state ds ON ds.device_id = d.id
      LEFT JOIN model md    ON md.id = d.model_id
      LEFT JOIN rack r      ON r.id = d.rack_id
      LEFT JOIN rack_row rr ON rr.id = r.row_id
      LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
      LEFT JOIN datacenter dc ON dc.id = rm.datacenter_id
     WHERE d.lifecycle <> 'decommissioned'
       AND """ + _FACILITY_IN_FACILITY_ROOMS + """
     ORDER BY dc.code, d.name
""")

#: The newest sample per (device, key, instance) inside a short window.
#:
#: Bounded so a machine that stopped reporting does not present a stale
#: temperature as current: an empty row is a fact about the machine, a stale
#: one is a lie about the plant.
#:
#: TEN minutes, matching repositories/cooling.py. Plant points are polled every
#: thirty to a hundred and twenty seconds, so ten minutes is already five
#: missed polls - and the window is the scan: at thirty it was two thirds of
#: this endpoint's entire response time, walking rows that a device reporting
#: normally had superseded eight times over.
_LATEST = text("""
    SELECT DISTINCT ON (t.device_id, m.key, t.instance)
           t.device_id::text AS device_id, m.key, t.instance, t.value
      FROM telemetry_sample t
      JOIN metric m ON m.id = t.metric_id
      JOIN device d ON d.id = t.device_id
      LEFT JOIN rack r      ON r.id = d.rack_id
      LEFT JOIN rack_row rr ON rr.id = r.row_id
      LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
     WHERE t.ts > now() - interval '10 minutes'
       AND d.lifecycle <> 'decommissioned'
       -- Per population: a chiller publishes no battery health and a
       -- switchboard no chilled-water temperature, so the union against every
       -- device walks twice the index for nothing.
       AND ( (d.device_type = ANY(:cool_types) AND m.key = ANY(:cool_keys))
          OR (d.device_type = ANY(:fac_types) AND m.key = ANY(:fac_keys)
              AND rm.room_class = 'facility') )
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


#: The bind parameters both filtered queries need.
_SCOPE = {"cool_types": PLANT_TYPES, "fac_types": FACILITY_TYPES}


async def machines(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(_MACHINES, _SCOPE)).mappings().all()
    return [dict(r) for r in rows]


async def latest(session: AsyncSession) -> dict[str, dict[tuple[str, str], float]]:
    """{device_id: {(key, instance): value}} - the newest of each."""
    rows = (await session.execute(_LATEST, {
        **_SCOPE, "cool_keys": list(_KEYS), "fac_keys": list(_FACILITY_KEYS),
    })).mappings().all()
    out: dict[str, dict[tuple[str, str], float]] = {}
    for r in rows:
        out.setdefault(r["device_id"], {})[
            (r["key"], r["instance"] or "")] = float(r["value"])
    return out


async def flags(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """{device_id: {"running", "states": {instance: bool}, "alarm_points": [...]}}.

    `running` is only meaningful where equipment_state has ONE instance, which
    is every cooling machine: Unit_Running, Chiller_Running, Fan_Status,
    Run_Status, Status_Modulating.

    The electrical gear publishes several at once and none of them means
    "running": a UPS carries On_Battery and Bypass_Active, an ATS carries
    On_Emergency, Normal_Available and Emergency_Available. Collapsing those
    to one boolean would report a UPS that has dropped to battery as either
    running or stopped, when the fact worth knowing is neither. So every
    instance is kept and the service reads the ones its type defines.
    """
    rows = (await session.execute(
        _FLAGS, {"window_s": LAST_KNOWN_WINDOW_S,
                 "types": ALL_TYPES})).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = out.setdefault(r["device_id"], {
            "running": None, "states": {}, "alarm_points": []})
        if r["key"] == "equipment_state":
            d["states"][r["instance"] or ""] = bool(r["value"])
        elif r["value"]:
            d["alarm_points"].append(r["instance"] or "alarm")
    for d in out.values():
        if len(d["states"]) == 1:
            d["running"] = next(iter(d["states"].values()))
    return out


async def alarms(session: AsyncSession) -> dict[str, dict[str, int]]:
    rows = (await session.execute(
        _ALARMS, {"types": ALL_TYPES})).mappings().all()
    return {r["device_id"]: {"open": int(r["n"]), "worst": int(r["worst"])}
            for r in rows}


async def observed_kw(session: AsyncSession) -> dict[str, float]:
    rows = (await session.execute(
        _OBSERVED_KW, {"types": ALL_TYPES})).mappings().all()
    return {r["device_id"]: float(r["rated_w"]) / 1000.0 for r in rows}
