"""A room at one moment in the past (docs/27 D8, Phase 4).

Playback reads frames, and a frame is the live scene's shape filled from
history: the geometry is what the room IS (an import, not telemetry, D4), the
readings are what the sensors SAID at that moment, the severities are what
was open then. The viewer cannot tell a frame from the live scene, which is
the point - the same picture, the same layers, the same indices.

Where a reading comes from is decided by how old the moment is, the same
rule the thermal trend uses:

  * the newest two hours: the hypertable itself. A rollup cannot be more
    current than its refresh policy (five-minute every five minutes, hourly
    every thirty) and the newest hour is the one somebody replays after an
    incident;
  * up to two days: the five-minute rollup, `last_value` in the bucket;
  * older: the hourly rollup, kept for ever.

A reading is "at t" if it is the last one at or before t within a lookback
of a few buckets. A sensor that said nothing in that window has no reading
in the frame, and the rack it is in is drawn grey there rather than with the
value it had an hour earlier.

What a frame does NOT carry: communication status. `poll_result` keeps two
weeks and is per endpoint, not per device; a historical frame shows every
device as it is known NOW, and the payload says so.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.schemas import FloorEquipment, FloorPlan, FloorRack, TwinDevice

#: Metric keys a frame reads, and the field each one fills.
DEVICE_KEYS = ("inlet_temperature", "exhaust_temperature", "ambient_temperature",
               "relative_humidity", "power_draw", "supply_air_temp", "return_air_temp")

RAW_HORIZON = timedelta(hours=2)
FIVE_MIN_HORIZON = timedelta(days=2)


@dataclass(frozen=True)
class FrameSource:
    table: str
    #: How far back of `t` the last reading may lie and still count.
    lookback: timedelta
    label: str


def choose_source(t: datetime, now: datetime) -> FrameSource:
    age = now - t
    if age <= RAW_HORIZON:
        return FrameSource("telemetry_sample", timedelta(minutes=15), "raw")
    if age <= FIVE_MIN_HORIZON:
        return FrameSource("telemetry_5m", timedelta(minutes=15), "5m")
    return FrameSource("telemetry_1h", timedelta(hours=3), "1h")


#: Worst last, the order the alarm engine's enum declares.
SEVERITY_RANK = {"CLEAR": 0, "INFO": 1, "WARNING": 2, "MINOR": 3, "MAJOR": 4, "CRITICAL": 5}


def _worst(a: str, b: str) -> str:
    return a if SEVERITY_RANK.get(a, 0) >= SEVERITY_RANK.get(b, 0) else b


def device_at(d: TwinDevice, v: dict[str, float], severity: str | None) -> TwinDevice:
    """The device with its readings replaced by the frame's. Status is kept
    as known now (see module doc); severity is what was open at t."""
    inlet = v.get("inlet_temperature")
    ambient = v.get("ambient_temperature")
    return d.model_copy(update={
        "inlet_c": inlet,
        "temp_c": inlet if inlet is not None else ambient,
        "rh_pct": v.get("relative_humidity"),
        "power_w": v.get("power_draw"),
        "exhaust_c": v.get("exhaust_temperature"),
        "max_severity": severity or "CLEAR",
    })


def equipment_at(e: FloorEquipment, v: dict[str, float], severity: str | None) -> FloorEquipment:
    return e.model_copy(update={
        "power_w": v.get("power_draw"),
        "inlet_c": v.get("inlet_temperature"),
        "supply_c": v.get("supply_air_temp"),
        "return_c": v.get("return_air_temp"),
        "temp_c": v.get("ambient_temperature"),
        "rh_pct": v.get("relative_humidity"),
        "max_severity": severity or "CLEAR",
    })


def rack_at(r: FloorRack, devices: list[TwinDevice]) -> FloorRack:
    """The rack's roll-up from its devices at t: load is what its rack PDUs
    metered (the live rule - summing every device counts each server twice),
    the hottest intake, the worst open condition. Offline counts are a
    communication state and have no history, so the frame says none."""
    pdu_w = [d.power_w for d in devices if d.device_type == "pdu" and d.power_w is not None]
    inlets = [d.inlet_c for d in devices if d.inlet_c is not None]
    worst = "CLEAR"
    for d in devices:
        worst = _worst(worst, d.max_severity)
    return r.model_copy(update={
        "load_kw": (sum(pdu_w) / 1000.0) if pdu_w else None,
        "max_inlet_c": max(inlets) if inlets else None,
        "max_severity": worst,
        "offline_count": 0,
    })


def apply(plan: FloorPlan, devices: list[TwinDevice],
          values: dict[str, dict[str, float]],
          severities: dict[str, str]) -> tuple[FloorPlan, list[TwinDevice]]:
    """The live scene re-filled from `values` ({device_id: {key: value}}) and
    `severities` ({device_id: worst open severity at t})."""
    devs = [device_at(d, values.get(d.id, {}), severities.get(d.id)) for d in devices]
    by_rack: dict[str, list[TwinDevice]] = {}
    for d in devs:
        by_rack.setdefault(d.rack_id, []).append(d)
    racks = [rack_at(r, by_rack.get(r.id, [])) for r in plan.racks]
    eq = [equipment_at(e, values.get(e.id, {}), severities.get(e.id)) for e in plan.equipment]
    loose = [equipment_at(e, values.get(e.id, {}), severities.get(e.id))
             for e in plan.unpositioned_equipment]
    return plan.model_copy(update={"racks": racks, "equipment": eq,
                                   "unpositioned_equipment": loose}), devs
