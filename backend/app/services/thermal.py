"""Thermal analytics: rack ΔT, hot spots, and what a hot CRAH actually means.

The distinction this exists to make is between two readings that look similar
on a dashboard and send an engineer to opposite ends of the building.

A CRAH with a **high return** is being fed hot air by the room. The unit is
working; the hot aisle reaching it is too hot. That is a load or a containment
problem - a missing blanking panel, a bypass, more kilowatts in the row than the
row was built for - and the fix is on the floor.

A CRAH with a **high supply** has failed to cool. Whatever is entering it, it is
not delivering cold air: a stuck chilled-water valve, a fouled coil, no flow.
The fix is at the unit.

Getting these the wrong way round means dispatching someone to inspect a healthy
machine while the actual load problem keeps growing, so they are classified
separately and never merged into a single "CRAH hot" state.

Hot spots are judged on INLET temperature, because that is what the equipment
breathes and what ASHRAE governs, and relative to the room rather than against
an absolute. A rack running 4 K hotter than its neighbours is a finding even
while every reading is still inside the allowable envelope - that is the point
at which it is cheap to fix.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

# ASHRAE A1: 18-27 C recommended intake, 32 C allowable.
ASHRAE_RECOMMENDED_MAX = 27.0
ASHRAE_ALLOWABLE_MAX = 32.0

# A rack this far above the room's 90th percentile inlet is a hot spot, even
# when it is still within the envelope. Relative, because a room running warm
# everywhere is a different problem from one rack running warm.
HOT_SPOT_MARGIN_K = 3.0

# How long the condition must hold. A single hot sample is a fan ramp or a
# sensor blip; fifteen minutes is a thermal event.
SUSTAINED_MINUTES = 15

# A CRAH that cannot hold its discharge this far above setpoint has stopped
# cooling, whatever its return reads.
SUPPLY_FAULT_MARGIN_K = 3.0

# Return air this far above the room's own 90th percentile return marks the
# unit taking the hottest air - a load or containment signal, not a fault.
RETURN_HIGH_MARGIN_K = 3.0

# A hot aisle above this is abnormal on its own terms, whatever the other units
# read. The relative test alone is blind to the case where EVERY unit is taking
# hot air, which is exactly what a room-wide cooling shortfall looks like.
RETURN_HIGH_ABSOLUTE_C = 32.0

# Share of racks above the recommended envelope that turns "some warm racks"
# into a room-wide event. Different cause and different runbook from a hot
# spot: one rack hotter than its neighbours is airflow, a whole room drifting
# up is plant or capacity.
ROOM_EVENT_FRACTION = 0.5


@dataclass
class RackThermal:
    rack_id: str | None
    name: str
    inlet_mean: float | None = None
    inlet_min: float | None = None
    inlet_max: float | None = None
    exhaust_mean: float | None = None
    samples: int = 0

    @property
    def delta_t_k(self) -> float | None:
        """Exhaust minus intake: how much heat the air actually carried away.

        Low ΔT on a loaded rack means more air is being pushed through it than
        the load needs - bypass - which wastes fan energy and steals cold air
        from racks that do need it. High ΔT means the opposite.
        """
        if self.inlet_mean is None or self.exhaust_mean is None:
            return None
        return self.exhaust_mean - self.inlet_mean

    @property
    def above_recommended(self) -> bool:
        return (self.inlet_mean or 0) > ASHRAE_RECOMMENDED_MAX

    @property
    def above_allowable(self) -> bool:
        return (self.inlet_mean or 0) > ASHRAE_ALLOWABLE_MAX


@dataclass
class CrahThermal:
    device_id: str
    name: str
    supply_c: float | None = None
    return_c: float | None = None
    setpoint_c: float | None = None
    running: bool | None = None

    @property
    def delta_t_k(self) -> float | None:
        if self.supply_c is None or self.return_c is None:
            return None
        return self.return_c - self.supply_c


def classify_crah(unit: CrahThermal, room_return_p90: float | None
                  ) -> tuple[str, str | None]:
    """high_supply | high_return | ok | unknown, and why.

    Supply is checked first and wins. A unit that is not delivering cold air is
    broken whatever its return reads, and a high return on top of that is a
    consequence, not a second finding.
    """
    if unit.running is False:
        # Its last supply reading is whatever it was delivering when it
        # stopped, so grading it against setpoint would report a dead unit as
        # healthy - which is how five stopped CRAHs read as "ok" during a real
        # room event.
        return "stopped", "not running; its air temperatures are stale"

    if unit.supply_c is None and unit.return_c is None:
        return "unknown", "no air temperatures reported"

    if (unit.supply_c is not None and unit.setpoint_c is not None
            and unit.supply_c > unit.setpoint_c + SUPPLY_FAULT_MARGIN_K):
        return "high_supply", (
            f"discharging {unit.supply_c:.1f} C against a {unit.setpoint_c:.1f} C "
            f"setpoint - the unit is not cooling; check chilled water, valve "
            f"and coil, not the floor")

    if unit.return_c is not None:
        relative = (room_return_p90 is not None
                    and unit.return_c > room_return_p90 + RETURN_HIGH_MARGIN_K)
        absolute = unit.return_c > RETURN_HIGH_ABSOLUTE_C
        if relative or absolute:
            against = (f"a room p90 of {room_return_p90:.1f} C" if relative
                       else f"an absolute limit of {RETURN_HIGH_ABSOLUTE_C:.0f} C")
            return "high_return", (
                f"taking {unit.return_c:.1f} C return air against {against} - "
                f"the hot aisle feeding it is too hot, which is a load or "
                f"containment problem, not a unit fault")

    return "ok", None


def hot_spots(racks: list[RackThermal], p90: float | None,
              margin: float = HOT_SPOT_MARGIN_K) -> list[dict[str, Any]]:
    """Racks running hot relative to the room, sustained.

    ``inlet_min`` is the test, not the mean: the minimum over the window means
    the rack was above the threshold for the WHOLE window rather than averaging
    above it after one spike. That is what "sustained" has to mean or the
    detector fires on transients.
    """
    if p90 is None:
        return []
    threshold = p90 + margin
    out = []
    for r in racks:
        if r.inlet_min is None or r.samples == 0:
            continue
        if r.inlet_min > threshold:
            out.append({
                "rack_id": r.rack_id, "name": r.name,
                "inlet_mean": round(r.inlet_mean, 1) if r.inlet_mean else None,
                "inlet_min": round(r.inlet_min, 1),
                "inlet_max": round(r.inlet_max, 1) if r.inlet_max else None,
                "threshold": round(threshold, 1),
                "over_by_k": round(r.inlet_min - threshold, 1),
                "above_recommended": r.above_recommended,
                "above_allowable": r.above_allowable,
                "samples": r.samples,
            })
    return sorted(out, key=lambda h: -h["over_by_k"])


@dataclass
class RoomThermal:
    room_id: str
    name: str | None = None
    racks: list[RackThermal] = field(default_factory=list)
    crahs: list[CrahThermal] = field(default_factory=list)
    inlet_p90: float | None = None
    return_p90: float | None = None
    window_minutes: int = SUSTAINED_MINUTES

    def as_dict(self) -> dict[str, Any]:
        spots = hot_spots(self.racks, self.inlet_p90)
        units = []
        for u in self.crahs:
            kind, why = classify_crah(u, self.return_p90)
            units.append({
                "device_id": u.device_id, "name": u.name, "state": kind,
                # BACnet reals arrive as float32, so 27.6 widens to
                # 27.600000381469727 on the way out. One decimal is finer than
                # any of these sensors is accurate to.
                "reason": why, "supply_c": _r(u.supply_c), "return_c": _r(u.return_c),
                "setpoint_c": _r(u.setpoint_c), "delta_t_k": (
                    round(u.delta_t_k, 1) if u.delta_t_k is not None else None),
                "running": u.running,
            })
        supplies = [u.supply_c for u in self.crahs if u.supply_c is not None]
        returns = [u.return_c for u in self.crahs if u.return_c is not None]
        # A room-wide event, which the hot-spot test cannot see: when every
        # rack drifts up together the p90 drifts with them and nothing is
        # "relatively" hot. Losing five of seven CRAHs put this room's whole
        # inlet population above the recommended envelope while hot_spots
        # correctly reported none.
        warm = [r for r in self.racks if r.above_recommended]
        over = [r for r in self.racks if r.above_allowable]
        event = None
        if self.racks and len(warm) >= max(1, int(len(self.racks) * ROOM_EVENT_FRACTION)):
            worst = max(self.racks, key=lambda r: r.inlet_mean or 0)
            event = {
                "kind": "room_over_temperature",
                "racks_above_recommended": len(warm),
                "racks_above_allowable": len(over),
                "racks_total": len(self.racks),
                "hottest_rack": worst.name,
                "hottest_inlet_c": round(worst.inlet_mean, 1) if worst.inlet_mean else None,
                "reason": (
                    f"{len(warm)} of {len(self.racks)} racks are drawing air above "
                    f"{ASHRAE_RECOMMENDED_MAX:.0f} C"
                    + (f", {len(over)} above the {ASHRAE_ALLOWABLE_MAX:.0f} C "
                       f"allowable limit" if over else "")
                    + " - the whole room is warm, so this is plant or capacity "
                      "rather than a local airflow problem"),
            }

        return {
            "room_id": self.room_id,
            "name": self.name,
            "thermal_event": event,
            "window_minutes": self.window_minutes,
            "inlet_p90_c": round(self.inlet_p90, 1) if self.inlet_p90 else None,
            "hot_spot_threshold_c": (
                round(self.inlet_p90 + HOT_SPOT_MARGIN_K, 1)
                if self.inlet_p90 else None),
            "hot_spots": spots,
            "hot_spot_count": len(spots),
            "room_delta_t_k": (
                round(sum(returns) / len(returns) - sum(supplies) / len(supplies), 1)
                if supplies and returns else None),
            "crah_units": units,
            "units_high_supply": sum(1 for u in units if u["state"] == "high_supply"),
            "units_high_return": sum(1 for u in units if u["state"] == "high_return"),
            "racks": [
                {
                    "rack_id": r.rack_id, "name": r.name,
                    "inlet_mean_c": round(r.inlet_mean, 1) if r.inlet_mean else None,
                    "exhaust_mean_c": (round(r.exhaust_mean, 1)
                                       if r.exhaust_mean else None),
                    "delta_t_k": (round(r.delta_t_k, 1)
                                  if r.delta_t_k is not None else None),
                    "above_recommended": r.above_recommended,
                    "above_allowable": r.above_allowable,
                }
                for r in sorted(self.racks, key=lambda x: -(x.inlet_mean or 0))
            ],
        }


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile. Small samples here - a room has tens of racks,
    not thousands - so an exact method beats an interpolated one.

    ceil, not round: nearest rank is ceil(pct/100 * n), and round() breaks ties
    to even, so an exact rank of 3.0 would have picked the 4th value while 2.0
    picked the 2nd. On a 20-rack room that is a whole rack of drift in the
    baseline every hot-spot threshold is measured from.
    """
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, math.ceil(pct / 100.0 * len(vals)) - 1))
    return vals[k]


async def room_view(session, room_id: str,
                    minutes: int = SUSTAINED_MINUTES) -> dict[str, Any]:
    from app.repositories import thermal as repo

    rack_rows = await repo.racks(session, room_id=room_id, minutes=minutes)
    crah_rows = await repo.crahs(session, room_id=room_id)
    running = await repo.running_crahs(session, room_id)

    racks = [
        RackThermal(
            rack_id=r["rack_id"], name=r["name"],
            inlet_mean=_f(r["inlet_mean"]), inlet_min=_f(r["inlet_min"]),
            inlet_max=_f(r["inlet_max"]), exhaust_mean=_f(r["exhaust_mean"]),
            samples=int(r["samples"] or 0),
        )
        for r in rack_rows
    ]
    units = [
        CrahThermal(
            device_id=c["device_id"], name=c["name"],
            supply_c=_f(c["supply_c"]), return_c=_f(c["return_c"]),
            setpoint_c=_f(c["setpoint_c"]),
            running=running.get(c["device_id"]),
        )
        for c in crah_rows
    ]

    name = (await session.execute(
        text("SELECT name FROM room WHERE id = CAST(:id AS uuid)"),
        {"id": room_id})).scalar()
    view = RoomThermal(
        room_id=room_id, name=name, racks=racks, crahs=units,
        window_minutes=minutes,
        # The room baseline is built from rack MEANS, so one rack's spike does
        # not lift the threshold it is about to be measured against.
        inlet_p90=percentile([r.inlet_mean for r in racks if r.inlet_mean], 90),
        return_p90=percentile([u.return_c for u in units if u.return_c], 90),
    )
    return view.as_dict()


def _r(v: float | None, places: int = 1) -> float | None:
    """Round for presentation, keeping None as None."""
    return None if v is None else round(v, places)


def _f(v: Any) -> float | None:
    return None if v is None else float(v)
