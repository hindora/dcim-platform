"""Cooling analytics: loop ΔT, plant capacity against load, chiller staging.

Everything here rests on one equation and on being honest about where each
number came from.

    Q(kW) = flow(L/s) x ΔT(K) x cp

For water cp is 4.187 kJ/(kg.K) and density is close enough to 1 kg/L at loop
temperatures that the two cancel. That gives the heat a loop is ACTUALLY moving,
which is a different thing from the chiller's nameplate - a 800 kW machine
removing 113 kW is not "800 kW of cooling", and conflating the two is how a
plant looks fine right up until it does not.

There are two independent ways to get a chiller's current output: from the
water side (flow x ΔT) and from the electrical side (COP x input power). They
should agree. When they do, the reading is trustworthy; when they do not,
something is wrong with a sensor or with the model, and saying so is more
useful than picking one and moving on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Specific heat of water, kJ/(kg.K). Density at loop temperatures is ~1 kg/L,
# so litres per second and kilograms per second are interchangeable here.
WATER_CP = 4.187

# Below this, a chilled-water loop is not really transferring heat: it is
# circulating water past a coil. Low ΔT is the classic chilled-water plant
# pathology - excess flow, an open bypass, a fouled or oversized coil - and it
# wastes pump energy while capping the plant's usable capacity.
#
# Not a design ΔT, which this data does not carry. It is the value below which
# something is clearly wrong regardless of design.
LOW_DELTA_T_K = 3.0

# How far the water-side and electrical-side estimates of a chiller's output may
# differ before it is worth reporting. Sensor noise and the fact that the two
# are sampled at slightly different moments account for a few percent.
OUTPUT_AGREEMENT_PCT = 15.0

# Below this a loop is not circulating: pump off, valve shut, machine staged
# down. Diagnostics that only make sense on a moving loop are suppressed.
MIN_FLOW_L_S = 0.1


def heat_kw(flow_l_s: float | None, delta_t: float | None) -> float | None:
    """Heat moved by a water loop."""
    if flow_l_s is None or delta_t is None:
        return None
    return flow_l_s * delta_t * WATER_CP


def delta_t(supply: float | None, return_: float | None) -> float | None:
    """Return minus supply.

    Signed deliberately. A negative ΔT on a chilled-water loop means the return
    is colder than the supply, which is not a small error - it is a swapped
    sensor pair or a reversed flow, and clamping it to zero would hide that.
    """
    if supply is None or return_ is None:
        return None
    return return_ - supply


@dataclass
class Loop:
    name: str
    supply_c: float | None = None
    return_c: float | None = None
    flow_l_s: float | None = None

    @property
    def delta_t_k(self) -> float | None:
        return delta_t(self.supply_c, self.return_c)

    @property
    def heat_kw(self) -> float | None:
        return heat_kw(self.flow_l_s, self.delta_t_k)

    @property
    def circulating(self) -> bool:
        """Is water actually moving through this loop."""
        return self.flow_l_s is not None and self.flow_l_s > MIN_FLOW_L_S

    @property
    def low_delta_t(self) -> bool:
        """Low ΔT syndrome, which only means anything on a running loop.

        A stopped machine reads ΔT 0 because nothing is circulating, not
        because heat transfer has failed. Flagging it would put four healthy
        standby units on a fault list and teach people the flag is noise.
        """
        dt = self.delta_t_k
        return self.circulating and dt is not None and dt < LOW_DELTA_T_K


@dataclass
class Chiller:
    device_id: str
    name: str
    status: str = "UNKNOWN"
    running: bool | None = None
    rated_kw: float | None = None
    compressor_load_pct: float | None = None
    power_kw: float | None = None
    cop: float | None = None
    chw: Loop | None = None
    cond: Loop | None = None

    @property
    def output_thermal_kw(self) -> float | None:
        """From the water side: what the evaporator actually removed."""
        return self.chw.heat_kw if self.chw else None

    @property
    def output_electrical_kw(self) -> float | None:
        """From the electrical side: COP is output over input, by definition."""
        if self.cop is None or self.power_kw is None:
            return None
        return self.cop * self.power_kw

    @property
    def output_disagreement_pct(self) -> float | None:
        a, b = self.output_thermal_kw, self.output_electrical_kw
        if a is None or b is None or max(a, b) <= 0:
            return None
        return abs(a - b) / max(a, b) * 100.0

    @property
    def load_pct(self) -> float | None:
        """Against nameplate, not against whatever it happens to be doing."""
        out = self.output_thermal_kw
        if out is None or not self.rated_kw:
            return None
        return out / self.rated_kw * 100.0


@dataclass
class PlantView:
    room: str | None = None
    chillers: list[Chiller] = field(default_factory=list)
    loops: list[Loop] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def running(self) -> list[Chiller]:
        return [c for c in self.chillers if c.running]

    @property
    def standby(self) -> list[Chiller]:
        return [c for c in self.chillers if c.running is False]

    @property
    def load_kw(self) -> float:
        return sum(c.output_thermal_kw or 0.0 for c in self.running)

    @property
    def running_capacity_kw(self) -> float:
        return sum(c.rated_kw or 0.0 for c in self.running)

    @property
    def installed_capacity_kw(self) -> float:
        return sum(c.rated_kw or 0.0 for c in self.chillers)


def staging_verdict(plant: PlantView) -> tuple[str, str]:
    """Can the plant lose its largest running machine and still carry the load.

    The standard N+1 test for a chiller plant, and it is about the RUNNING set:
    a standby machine that has to start, pull down and stage on does not help in
    the minutes after a trip, so redundancy is judged on what is already turning
    and reported separately from what could be started.
    """
    running = plant.running
    if not running:
        return "no_capacity", "no chiller is running"

    load = plant.load_kw
    largest = max((c.rated_kw or 0.0) for c in running)
    surviving = plant.running_capacity_kw - largest

    if load <= 0:
        return "idle", "the plant is running but moving no measurable heat"
    if surviving >= load:
        return "N+1", (
            f"{len(running)} running; losing the largest ({largest:.0f} kW) "
            f"still leaves {surviving:.0f} kW against a {load:.0f} kW load")
    if plant.running_capacity_kw >= load:
        short = load - surviving
        return "N", (
            f"{len(running)} running carry {load:.0f} kW, but losing the largest "
            f"leaves {surviving:.0f} kW - {short:.0f} kW short"
            + (f"; {len(plant.standby)} standby machine(s) available to start"
               if plant.standby else " and nothing on standby"))
    return "over_capacity", (
        f"load {load:.0f} kW exceeds the {plant.running_capacity_kw:.0f} kW "
        f"currently running")


def data_quality(plant: PlantView) -> list[str]:
    """Readings that do not add up, stated rather than smoothed over."""
    out: list[str] = []
    for c in plant.chillers:
        d = c.output_disagreement_pct
        if d is not None and d > OUTPUT_AGREEMENT_PCT:
            out.append(
                f"{c.name}: water side says {c.output_thermal_kw:.0f} kW, "
                f"electrical side says {c.output_electrical_kw:.0f} kW "
                f"({d:.0f}% apart) - one of flow, ΔT, COP or input power is wrong")
        if c.chw and c.chw.delta_t_k is not None and c.chw.delta_t_k < 0:
            out.append(
                f"{c.name}: chilled-water return is colder than supply "
                f"({c.chw.delta_t_k:.1f} K) - swapped sensors or reversed flow")
        if c.running and c.chw and c.chw.low_delta_t:
            out.append(
                f"{c.name}: chilled-water ΔT is {c.chw.delta_t_k:.1f} K, below "
                f"{LOW_DELTA_T_K} K - excess flow or a bypass, which wastes pump "
                f"energy and caps usable capacity")
    return out


def summarise(plant: PlantView) -> dict[str, Any]:
    kind, reason = staging_verdict(plant)
    return {
        "staging": kind,
        "reason": reason,
        "load_kw": round(plant.load_kw, 1),
        "running_capacity_kw": round(plant.running_capacity_kw, 1),
        "installed_capacity_kw": round(plant.installed_capacity_kw, 1),
        "running": len(plant.running),
        "standby": len(plant.standby),
        # How much of the installed figure is actually known. A machine that has
        # not run in a day has no nameplate here, and reporting installed
        # capacity without saying so invites reading it as the whole plant.
        "nameplate_unknown": sum(1 for c in plant.chillers if c.rated_kw is None),
        "utilisation_pct": (
            round(plant.load_kw / plant.running_capacity_kw * 100.0, 1)
            if plant.running_capacity_kw else None),
        "data_quality": data_quality(plant),
    }


async def plant_view(session, room_id: str | None = None) -> dict[str, Any]:
    """Assemble the plant picture from the latest stored telemetry."""
    from app.repositories import cooling as repo

    rows = await repo.latest(session)
    flags = await repo.machine_flags(session)
    # Nameplate, which the live capacity point cannot give for a stopped
    # machine because it reads zero when off.
    nameplate = await repo.nameplate_kw(session)

    # (device, metric, instance) -> value, plus the device's identity once.
    by_dev: dict[str, dict[str, Any]] = {}
    for r in rows:
        if room_id and r.get("room_id") != room_id:
            continue
        d = by_dev.setdefault(r["device_id"], {
            "name": r["name"], "device_type": r["device_type"],
            "status": r["status"], "room_id": r.get("room_id"),
            "room_name": r.get("room_name"), "v": {},
        })
        d["v"][(r["key"], r["instance"] or "")] = r["value"]

    chillers: list[Chiller] = []
    loops: list[Loop] = []
    for dev_id, d in by_dev.items():
        v = d["v"]
        state = flags.get(dev_id, {})

        # v is bound as a default so the closure reads THIS device's values,
        # not whichever device the loop happens to be on when it is called.
        def val(key: str, inst: str = "", _v: dict = v) -> float | None:
            got = _v.get((key, inst))
            return float(got) if got is not None else None

        if d["device_type"] == "chiller":
            chw = Loop("CHW", val("water_supply_temp", "CHW"),
                       val("water_return_temp", "CHW"), val("water_flow", "CHW"))
            cond = Loop("COND", val("water_supply_temp", "COND"),
                        val("water_return_temp", "COND"),
                        val("water_flow", "COND"))
            power = val("power_draw")
            chillers.append(Chiller(
                device_id=dev_id, name=d["name"], status=d["status"],
                running=state.get("equipment_state"),
                # Nameplate, not the live capacity point: that reads zero on a
                # stopped machine, which would erase standby capacity from the
                # plant total exactly when someone is asking whether it exists.
                rated_kw=nameplate.get(dev_id),
                power_kw=power / 1000.0 if power is not None else None,
                compressor_load_pct=val("compressor_load_pct"),
                cop=val("cop"), chw=chw, cond=cond))
        elif d["device_type"] in ("cdu", "cooling_tower"):
            for inst in {i for (_, i) in v}:
                sup = val("water_supply_temp", inst)
                ret = val("water_return_temp", inst)
                if sup is None and ret is None:
                    continue
                loops.append(Loop(f"{d['name']}/{inst or 'loop'}", sup, ret,
                                  val("water_flow", inst)))

    plant = PlantView(room=None, chillers=sorted(chillers, key=lambda c: c.name),
                      loops=loops)
    return {"plant": plant, **summarise(plant)}
