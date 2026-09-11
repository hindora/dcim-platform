"""The cooling plant, read as a chain rather than as a list of machines.

The thermal page answers "is the air in the halls in band". This answers the
question underneath it: is the machinery that keeps it there intact, and how
much of it is spare. They are different jobs done by different people, which
is why a plant room full of dashes on a rack-intake table was never the
answer - a generator hall has no intake sensors and never will.

The chain, in the direction the heat travels:

    CRAH  ->  CDU  ->  pumps and valves  ->  chillers  ->  cooling towers
    (hall air)  (rack liquid)   (CHW distribution)   (CHW)      (COND, roof)

Each stage is summarised per SITE, never across sites. DC1's chilled-water
header is not DC2's, and a mean of the two describes no header that exists.
Within a site, temperatures pool only where the machines genuinely share a
header - the chillers on CHW, the towers on the condenser loop. CRAHs and CDUs
each own an independent loop, so those stages report a RANGE instead of a
mean: "12.9-13.4 K" is a fact, and the average of twelve separate loops is not.

Two independent readings of the same load are kept apart on purpose. The air
side is what the CRAHs and CDUs say they are delivering; the water side is
flow times delta-T at the chillers. They should agree within instrument error.
When they do not, one of the two is wrong, and saying so is worth more than
averaging them into a number that matches neither.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import plant as repo
from app.services.cooling import LOW_DELTA_T_K, MIN_FLOW_L_S, delta_t, heat_kw
from app.services.thermal import CrahThermal, classify_crah

#: A tower's approach is how far above the wet bulb it can deliver condenser
#: water. Design approach on an evaporative tower is typically 3-5 K; a tower
#: running much wider than that on a part-load day is fouled, scaled, short of
#: airflow, or short of water. Deliberately loose: this is the line above which
#: somebody should look, not a design figure the platform has on file.
APPROACH_LIMIT_K = 8.0

#: How far a modulating valve may sit from its commanded position before the
#: actuator is suspect. A BACnet valve reports both; the gap between them is
#: the only signal that the command is not being obeyed.
#:
#: Set well outside a positioner's normal deadband, which is a few percent on
#: a modulating actuator and is not a fault - a valve that hunts inside its
#: deadband is a valve working. This is the gap that means the stem is stuck,
#: the linkage has slipped or the actuator has lost air or power.
VALVE_DEVIATION_PCT = 10.0

#: Watts per kilowatt. Plant points are published in W, the page reads kW.
W_PER_KW = 1000.0

#: The chain, in the order the heat travels through it. `header` says whether
#: the machines of a stage share one set of pipes at a site: chillers and
#: towers do, so their loop temperatures may be pooled; every CRAH and CDU
#: owns its own loop, so theirs may only be ranged.
STAGES: tuple[tuple[str, str, str, bool, str], ...] = (
    ("crah", "CRAH", "crah", False,
     "Air handlers in the halls. They pick the heat up out of the room and "
     "put it into chilled water."),
    ("cdu", "CDU", "cdu", False,
     "Coolant distribution units. A second, warmer loop taken to the rack "
     "itself, isolated from the facility water by a heat exchanger."),
    ("pump", "Pumps", "pump", True,
     "Chilled- and condenser-water pumps. They move the water; they do not "
     "make it cold."),
    ("valve", "Valves", "valve", True,
     "Motorised isolation and bypass valves on the headers. Each reports "
     "what it was told and where it actually is."),
    ("chiller", "Chillers", "chiller", True,
     "The machines that actually make cold water. Redundancy is judged here."),
    ("cooling_tower", "Cooling towers", "cooling_tower", True,
     "Heat rejection to atmosphere. Judged against the wet bulb, because "
     "that is the temperature physics allows them to approach."),
)

_STAGE_BY_TYPE = {s[2]: s[0] for s in STAGES}
_STAGE_ORDER = {s[0]: i for i, s in enumerate(STAGES)}


def _val(v: dict[tuple[str, str], float], key: str, inst: str = "") -> float | None:
    got = v.get((key, inst))
    return float(got) if got is not None else None


def _kw(w: float | None) -> float | None:
    return None if w is None else w / W_PER_KW


def _r(x: float | None, places: int = 1) -> float | None:
    return None if x is None else round(x, places)


def _sum(values: list[float | None]) -> float | None:
    """Sum of what reported, or None when nothing did.

    Distinct from zero on purpose: a stage drawing no power and a stage that
    published no power reading are different findings, and folding them into
    the same 0.0 is how a dead instrument reads as an efficient machine.
    """
    got = [v for v in values if v is not None]
    return sum(got) if got else None


def _rated_kw(m: dict[str, Any], observed: dict[str, float]) -> float | None:
    """What this machine removes at full load, in kW.

    Model first: a datasheet figure is true whether the machine is running or
    staged off, and redundancy has to be judged on machines that are not
    running. The observed capacity point is the fallback for a SKU with no
    rating on file.
    """
    w = m.get("rated_cooling_w")
    if w:
        return float(w) / W_PER_KW
    return observed.get(m["device_id"])


# --------------------------------------------------------------------------
# One machine
# --------------------------------------------------------------------------

def _read_crah(v: dict, rated: float | None) -> dict[str, Any]:
    duty = _val(v, "cooling_output_pct")
    return {
        "supply_c": _val(v, "supply_air_temp"),
        "return_c": _val(v, "return_air_temp"),
        "setpoint_c": _val(v, "air_setpoint_temp"),
        "duty_pct": duty,
        "duty_of": "rated cooling",
        # Air-side delivery: the share of the nameplate the unit says it is
        # carrying. Not derivable from the air temperatures beside it, which
        # is why the machine publishes it.
        "heat_kw": None if duty is None or rated is None else duty / 100.0 * rated,
        "valve_pct": _val(v, "valve_position_pct", "CHW"),
        "fan_pct": _val(v, "fan_speed_pct"),
        "loop": "air",
    }


def _read_cdu(v: dict, rated: float | None) -> dict[str, Any]:
    sup, ret = _val(v, "water_supply_temp", "TCS"), _val(v, "water_return_temp", "TCS")
    flow = _val(v, "water_flow", "TCS")
    # The machine publishes the heat it is moving; flow x delta-T is the
    # check on it, not a substitute for it.
    heat = _kw(_val(v, "thermal_load"))
    if heat is None:
        heat = heat_kw(flow, delta_t(sup, ret))
    return {
        "supply_c": sup, "return_c": ret,
        "setpoint_c": _val(v, "water_setpoint_temp", "TCS"),
        "flow_l_s": flow,
        "heat_kw": heat,
        "duty_pct": None if heat is None or not rated else heat / rated * 100.0,
        "duty_of": "rated cooling",
        "valve_pct": _val(v, "valve_position_pct", "FACILITY_CHW"),
        "fan_pct": None,
        "pump_pct": _val(v, "pump_speed_pct"),
        "filter_dp": _val(v, "filter_diff_pressure"),
        "approach_k": _val(v, "approach_temp"),
        "loop": "water",
    }


def _read_pump(v: dict, rated: float | None) -> dict[str, Any]:
    speed = _val(v, "pump_speed_pct")
    return {
        "supply_c": None, "return_c": None, "setpoint_c": None,
        "flow_l_s": _val(v, "water_flow"),
        "diff_pressure": _val(v, "water_diff_pressure", "PUMP"),
        "heat_kw": None,
        "duty_pct": speed,
        "duty_of": "speed",
        "pump_pct": speed,
        "vfd_hz": _val(v, "vfd_frequency"),
        "motor_temp_c": _val(v, "motor_temp"),
        "loop": "none",
    }


def _read_valve(v: dict, rated: float | None) -> dict[str, Any]:
    commanded = _val(v, "valve_position_pct", "COMMANDED")
    measured = _val(v, "valve_position_pct", "MEASURED")
    return {
        # A valve has no temperature of its own, and the commanded position is
        # a percentage: putting it in the setpoint column would print "65 C"
        # under a temperature heading.
        "supply_c": None, "return_c": None, "setpoint_c": None,
        "heat_kw": None,
        "duty_pct": measured,
        "duty_of": "open",
        "valve_pct": measured,
        "commanded_pct": commanded,
        "deviation_pct": (None if commanded is None or measured is None
                          else abs(commanded - measured)),
        "motor_temp_c": _val(v, "motor_temp"),
        "loop": "none",
    }


def _read_chiller(v: dict, rated: float | None) -> dict[str, Any]:
    sup, ret = _val(v, "water_supply_temp", "CHW"), _val(v, "water_return_temp", "CHW")
    flow = _val(v, "water_flow", "CHW")
    power = _kw(_val(v, "power_draw"))
    cop = _val(v, "cop")
    water = heat_kw(flow, delta_t(sup, ret))
    electrical = None if cop is None or power is None else cop * power
    return {
        "supply_c": sup, "return_c": ret,
        "setpoint_c": _val(v, "water_setpoint_temp", "CHW"),
        "flow_l_s": flow,
        "heat_kw": water if water is not None else electrical,
        # Both estimates travel with the row. Where they disagree the page
        # says so rather than picking one.
        "heat_water_kw": water,
        "heat_electrical_kw": electrical,
        "duty_pct": None if water is None or not rated else water / rated * 100.0,
        "duty_of": "rated cooling",
        "compressor_pct": _val(v, "compressor_load_pct"),
        "cop": cop,
        "cond_supply_c": _val(v, "water_supply_temp", "COND"),
        "cond_return_c": _val(v, "water_return_temp", "COND"),
        "loop": "water",
    }


def _read_tower(v: dict, rated: float | None) -> dict[str, Any]:
    # Leaving water is what the tower produces; returning water is what the
    # condensers gave it back. Named supply/return in the plant's direction,
    # the same way every other stage on the page is named.
    sup, ret = _val(v, "water_supply_temp", "COND"), _val(v, "water_return_temp", "COND")
    wet = _val(v, "outdoor_wet_bulb_temp")
    fan = _val(v, "fan_speed_pct")
    return {
        "supply_c": sup, "return_c": ret, "setpoint_c": None,
        # No flow meter on the tower, so the heat it is rejecting cannot be
        # measured here. Left empty rather than inferred from the chillers,
        # which would report the plant's heat twice.
        "heat_kw": None,
        "duty_pct": fan,
        "duty_of": "fan speed",
        "fan_pct": fan,
        "wet_bulb_c": wet,
        "dry_bulb_c": _val(v, "outdoor_dry_bulb_temp"),
        "approach_k": None if sup is None or wet is None else sup - wet,
        "basin_pct": _val(v, "basin_level_pct"),
        "vibration": _val(v, "vibration"),
        "loop": "water",
    }


_READERS = {
    "crah": _read_crah, "cdu": _read_cdu, "pump": _read_pump,
    "valve": _read_valve, "chiller": _read_chiller, "cooling_tower": _read_tower,
}


def _verdict(m: dict[str, Any]) -> tuple[str, str | None]:
    """What is true about this machine, and what to do about it.

    Precedence is the order an engineer would triage in: something raised,
    then something not running, then something reporting nothing, then the
    performance findings that only mean anything on a machine that is working.
    """
    kind = m["device_type"]
    running = m["running"]
    open_alarms = m["alarms_open"]
    points = m["alarm_points"]

    if open_alarms and running is False:
        return "tripped", (f"not running with {open_alarms} open condition"
                           f"{'s' if open_alarms != 1 else ''}")
    if open_alarms:
        return "alarm", (f"{open_alarms} open condition"
                         f"{'s' if open_alarms != 1 else ''} on this machine")
    if points:
        # The machine is asserting a fault binary that no open condition
        # matches. Either a rule does not exist for it or a clear was missed -
        # both are worth a look, and neither is "healthy".
        return "fault_signal", (
            f"asserting {', '.join(sorted(points))} over BACnet with nothing "
            f"raised against it")
    if running is False:
        # Staged off means different things at different points in the chain.
        # A chiller plant is DESIGNED to run fewer machines than it owns, so a
        # quiet chiller is spare capacity. Every CRAH in a hall normally turns
        # together, so a quiet one is a unit that has dropped out - which is
        # the word the thermal page uses for it, and the two must not disagree.
        if kind in ("crah", "cdu"):
            return "stopped", "not running; its readings are stale"
        return "standby", "healthy and not running - capacity available to stage on"
    if running is None and m["heat_kw"] is None and m["supply_c"] is None:
        return "silent", "no run state and no readings inside the window"

    if kind == "crah":
        unit = CrahThermal(
            device_id=m["device_id"], name=m["name"], supply_c=m["supply_c"],
            return_c=m["return_c"], setpoint_c=m["setpoint_c"],
            valve_pct=m.get("valve_pct"), fan_pct=m.get("fan_pct"),
            duty_pct=m.get("duty_pct"), rated_kw=m.get("rated_kw"), running=running)
        # Judged against its own setpoint only. The room p90 test belongs to
        # the room table, which knows what the rest of the hall is returning.
        return classify_crah(unit, None)

    if kind == "valve":
        dev = m.get("deviation_pct")
        if dev is not None and dev > VALVE_DEVIATION_PCT:
            return "actuator", (
                f"commanded {m['commanded_pct']:.0f}% and sitting at "
                f"{m['valve_pct']:.0f}% - the actuator is not obeying")

    if kind == "cooling_tower":
        app = m.get("approach_k")
        if app is not None and app > APPROACH_LIMIT_K:
            return "high_approach", (
                f"delivering {m['supply_c']:.1f} C against a "
                f"{m['wet_bulb_c']:.1f} C wet bulb - a {app:.1f} K approach; "
                f"check fill, airflow and water distribution")
        basin = m.get("basin_pct")
        if basin is not None and basin < 40:
            return "low_basin", f"basin at {basin:.0f}% - make-up water is not keeping up"

    dt = m["delta_t_k"]
    if (m.get("loop") == "water" and dt is not None and dt < LOW_DELTA_T_K
            and (m.get("flow_l_s") is None or m["flow_l_s"] > MIN_FLOW_L_S)):
        return "low_delta_t", (
            f"{dt:.1f} K across a circulating loop, below {LOW_DELTA_T_K:.0f} K - "
            f"excess flow or a bypass; it wastes pump energy and caps usable "
            f"capacity")

    return "ok", None


def _machine_row(m: dict[str, Any], v: dict, f: dict, al: dict,
                 observed: dict[str, float]) -> dict[str, Any]:
    kind = m["device_type"]
    rated = _rated_kw(m, observed)
    row: dict[str, Any] = {
        # Both: `device_id` is what the rest of the platform calls it, `id` is
        # what the table component keys rows on.
        "id": m["device_id"],
        "device_id": m["device_id"],
        "name": m["name"],
        "device_type": kind,
        "stage": _STAGE_BY_TYPE[kind],
        "model": m.get("model_name"),
        "status": m.get("status"),
        "room_id": m.get("room_id"), "room_name": m.get("room_name"),
        "site_id": m.get("site_id"), "site_code": m.get("site_code"),
        "site_name": m.get("site_name"),
        "running": f.get("running"),
        "rated_kw": rated,
        "power_kw": _kw(_val(v, "power_draw")),
        "run_hours": _val(v, "run_hours"),
        "alarms_open": al.get("open", 0),
        "alarms_worst": al.get("worst", 0),
        "alarm_points": f.get("alarm_points", []),
    }
    row.update(_READERS[kind](v, rated))
    row["delta_t_k"] = delta_t(row.get("supply_c"), row.get("return_c"))
    verdict, why = _verdict(row)
    row["verdict"], row["why"] = verdict, why
    for k in ("supply_c", "return_c", "setpoint_c", "delta_t_k", "heat_kw",
              "power_kw", "duty_pct", "rated_kw", "valve_pct", "fan_pct",
              "pump_pct", "approach_k", "wet_bulb_c", "dry_bulb_c", "flow_l_s",
              "compressor_pct", "cop", "basin_pct", "commanded_pct",
              "deviation_pct", "heat_water_kw", "heat_electrical_kw",
              "diff_pressure", "motor_temp_c", "cond_supply_c", "cond_return_c",
              "filter_dp", "vfd_hz", "vibration", "run_hours"):
        if k in row:
            row[k] = _r(row[k], 2 if k in ("cop", "flow_l_s", "vibration") else 1)
    return row


# --------------------------------------------------------------------------
# One stage at one site
# --------------------------------------------------------------------------

#: Verdicts a stage inherits from its machines, worst first. A stage is as
#: healthy as its unhealthiest machine, except that standby is not a fault -
#: a plant with spare machines staged off is a plant working as designed.
_STAGE_PRECEDENCE = ("tripped", "alarm", "high_supply", "high_approach",
                     "low_basin", "actuator", "fault_signal", "low_delta_t",
                     "high_return", "silent", "unknown")


def _stage_row(key: str, label: str, header: bool, blurb: str,
               site: dict[str, Any], machines: list[dict[str, Any]]) -> dict[str, Any]:
    running = [m for m in machines if m["running"]]
    stopped = [m for m in machines if m["running"] is False]
    heats = [m["heat_kw"] for m in running if m["heat_kw"] is not None]
    rated_running = [m["rated_kw"] for m in running if m["rated_kw"]]
    rated_all = [m["rated_kw"] for m in machines if m["rated_kw"]]
    powers = [m["power_kw"] for m in machines if m["power_kw"] is not None]
    deltas = [m["delta_t_k"] for m in running if m["delta_t_k"] is not None]

    load = _sum(heats)
    cap_running = _sum(rated_running)

    row: dict[str, Any] = {
        "id": f"{site['site_id']}:{key}",
        "kind": "stage",
        "stage": key,
        "label": label,
        "blurb": blurb,
        "site_id": site["site_id"], "site_code": site["site_code"],
        "site_name": site["site_name"],
        "rooms": sorted({m["room_name"] for m in machines if m["room_name"]}),
        "machines": len(machines),
        "running": len(running),
        "standby": len([m for m in stopped if not m["alarms_open"]]),
        "stopped": len(stopped),
        "heat_kw": _r(load),
        "capacity_kw": _r(cap_running),
        "installed_kw": _r(_sum(rated_all)),
        "power_kw": _r(_sum(powers)),
        "duty_pct": _r(load / cap_running * 100.0 if load and cap_running else None),
        "alarms_open": sum(m["alarms_open"] for m in machines),
        # Pooled only where the machines share a header; otherwise the spread
        # across independent loops, which is a fact rather than an average of
        # unlike things.
        "delta_t_k": _r(sum(deltas) / len(deltas)) if header and deltas else None,
        "delta_t_min_k": _r(min(deltas)) if deltas else None,
        "delta_t_max_k": _r(max(deltas)) if deltas else None,
        "shared_header": header,
    }
    row["verdict"], row["why"] = _stage_verdict(row, machines, running)
    return row


def _stage_verdict(row: dict[str, Any], machines: list[dict[str, Any]],
                   running: list[dict[str, Any]]) -> tuple[str, str | None]:
    if machines and not running:
        return "no_capacity", (
            f"none of the {len(machines)} machines in this stage is running")

    worst = {m["verdict"] for m in machines}
    for v in _STAGE_PRECEDENCE:
        if v in worst:
            hit = [m["name"] for m in machines if m["verdict"] == v]
            shown = ", ".join(hit[:3]) + (f" +{len(hit) - 3}" if len(hit) > 3 else "")
            return v, f"{len(hit)} machine{'s' if len(hit) != 1 else ''}: {shown}"

    # Nothing is wrong with any individual machine, so the only question left
    # is whether the stage could lose one and carry on.
    load, cap = row["heat_kw"], row["capacity_kw"]
    if load and cap:
        largest = max((m["rated_kw"] or 0.0) for m in running)
        surviving = cap - largest
        if surviving >= load:
            return "n_plus_1", (
                f"{len(running)} running; losing the largest ({largest:.0f} kW) "
                f"still leaves {surviving:.0f} kW against a {load:.0f} kW load")
        return "tight", (
            f"{len(running)} running carry {load:.0f} kW; losing the largest "
            f"leaves {surviving:.0f} kW - {load - surviving:.0f} kW short"
            + (f", with {row['standby']} on standby to start"
               if row["standby"] else " and nothing on standby"))
    return "ok", None


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

def _notes(stages: list[dict[str, Any]], machines: list[dict[str, Any]],
           totals: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    notes.append(
        "The cooling chain, in the direction the heat travels: hall air into "
        "the CRAHs, rack liquid into the CDUs, both into chilled water, the "
        "chillers into the condenser loop, the towers into the sky. Each row "
        "is one stage at one site, because a site's header is its own.")

    air, water = totals.get("air_load_kw"), totals.get("water_load_kw")
    if air and water:
        gap = abs(air - water) / max(air, water) * 100.0
        if gap > 15:
            notes.append(
                f"The air side says {air:.0f} kW is being removed from the "
                f"halls and the water side says the chillers are moving "
                f"{water:.0f} kW - {gap:.0f}% apart. One of delivered duty, "
                f"loop flow or loop ΔT is wrong; the page will not average "
                f"them into a figure that matches neither.")
        else:
            notes.append(
                f"Air side {air:.0f} kW, water side {water:.0f} kW - two "
                f"independent measurements of the same load, agreeing within "
                f"{gap:.0f}%, so both can be trusted.")

    tight = [s for s in stages if s["verdict"] in ("tight", "no_capacity")]
    if tight:
        notes.append(
            "Redundancy is judged on what is already turning: "
            + "; ".join(f"{s['site_code']} {s['label'].lower()} - {s['why']}"
                        for s in tight[:3]))

    silent = [m for m in machines if m["verdict"] == "silent"]
    if silent:
        notes.append(
            f"{len(silent)} machine{'s' if len(silent) != 1 else ''} published "
            f"neither a run state nor a reading inside the window: "
            + ", ".join(m["name"] for m in silent[:4])
            + ". A machine nobody can hear is not a machine known to be off.")
    return notes


async def plant(session: AsyncSession) -> dict[str, Any]:
    """Every cooling machine in the estate, folded into the chain it belongs to."""
    inventory = await repo.machines(session)
    values = await repo.latest(session)
    flags = await repo.flags(session)
    alarms = await repo.alarms(session)
    observed = await repo.observed_kw(session)

    rows = [
        _machine_row(m, values.get(m["device_id"], {}),
                     flags.get(m["device_id"], {}),
                     alarms.get(m["device_id"], {}), observed)
        for m in inventory
        # A machine with no room has no site, and a plant table organised by
        # site cannot place it. Rare enough to be a data fault worth fixing
        # in inventory rather than papering over here.
        if m.get("site_id")
    ]

    sites: dict[str, dict[str, Any]] = {}
    for m in rows:
        sites.setdefault(m["site_id"], {
            "site_id": m["site_id"], "site_code": m["site_code"],
            "site_name": m["site_name"]})

    stages: list[dict[str, Any]] = []
    for site in sites.values():
        for key, label, dtype, header, blurb in STAGES:
            here = [m for m in rows
                    if m["site_id"] == site["site_id"] and m["device_type"] == dtype]
            if here:
                stages.append(_stage_row(key, label, header, blurb, site, here))
    stages.sort(key=lambda s: (s["site_code"] or "", _STAGE_ORDER[s["stage"]]))

    air = _sum([m["heat_kw"] for m in rows
                if m["device_type"] in ("crah", "cdu") and m["running"]])
    water = _sum([m["heat_kw"] for m in rows
                  if m["device_type"] == "chiller" and m["running"]])
    chillers = [m for m in rows if m["device_type"] == "chiller"]
    chill_stages = [s for s in stages if s["stage"] == "chiller"]

    totals = {
        "machines": len(rows),
        "running": sum(1 for m in rows if m["running"]),
        "standby": sum(1 for m in rows if m["running"] is False and not m["alarms_open"]),
        "stopped": sum(1 for m in rows if m["running"] is False),
        "silent": sum(1 for m in rows if m["verdict"] == "silent"),
        "air_load_kw": _r(air),
        "water_load_kw": _r(water),
        # Capacity is the chillers': a hall's worth of CRAHs cannot cool
        # anything the chillers are not making cold water for.
        "capacity_kw": _r(_sum([m["rated_kw"] for m in chillers if m["running"]])),
        "installed_kw": _r(_sum([m["rated_kw"] for m in chillers])),
        "power_kw": _r(_sum([m["power_kw"] for m in rows])),
        "alarms_open": sum(m["alarms_open"] for m in rows),
        # The estate reads as well as its worst site, not as well as its mean.
        "redundancy": _worst([s["verdict"] for s in chill_stages]),
        "sites": len(sites),
    }
    totals["utilisation_pct"] = _r(
        water / totals["capacity_kw"] * 100.0
        if water and totals["capacity_kw"] else None)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "stages": stages,
        "machines": rows,
        "totals": totals,
        "notes": _notes(stages, rows, totals),
    }


#: Worst to best, for folding several sites' verdicts into one headline.
_WORST_FIRST = ("no_capacity", "tripped", "alarm", "high_supply", "high_approach",
                "low_basin", "actuator", "fault_signal", "tight", "low_delta_t",
                "high_return", "silent", "unknown", "ok", "n_plus_1")


def _worst(verdicts: list[str]) -> str | None:
    for v in _WORST_FIRST:
        if v in verdicts:
            return v
    return None
