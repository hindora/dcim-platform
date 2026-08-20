"""Capacity: power, cooling, space and ports, and which one binds.

Four constraints, reported together, because the useful answer is not "this
room is at 40% power" - it is "this room runs out of SPACE first, and no amount
of power headroom changes that".

Two things this is careful about.

**p95 of the sum, not the sum of p95s.** Devices do not peak together. Adding
each device's 95th percentile assumes they do, and overstates the coincident
load - which is how rooms end up with stranded capacity that the spreadsheet
insists is in use. The percentile is taken over the summed load per interval.

**A missing rating is not a capacity of zero, and not a capacity of infinity.**
This fleet has no rack power or cooling ratings at all: rack.rated_power_kw and
rated_cool_kw are null on all 44 racks, and no PDU or RPP model carries a
rating either. A constraint with no known limit reports its usage, says the
limit is unknown, and is excluded from the binding calculation rather than
being quietly treated as unlimited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The percentile capacity planning sizes on. Sizing on the momentary peak
# strands capacity; sizing on the mean under-provisions for the hours that
# matter.
PERCENTILE = 95

# Utilisation past this is worth calling out even when nothing has bound yet -
# it is the point where an addition needs planning rather than a decision.
TIGHT_PCT = 80.0

# What the cooling plant actually has to remove. Not the whole facility: the
# chillers, pumps and CRAH fans are the cooling system, and counting their draw
# as a load on themselves inflates the heat they must reject by a third.
IT_TYPES = ["server", "switch", "router", "firewall", "load_balancer", "storage"]

# End devices only. Distribution gear is a conduit, not a load: a UPS reports
# the power flowing THROUGH it to the racks, so adding it to the racks' own
# draw counts the same kilowatts twice. Summing every device in DC1 gave
# 994 kW against an IT load of 104 kW - a facility ratio of nine, when the
# metered PUE is 1.42.
# Meters are excluded for the same reason as distribution gear, and it is easy
# to miss because they are not obviously "infrastructure": a branch-circuit
# monitor's power_draw is the power it MEASURES on its circuits, not what the
# meter itself consumes. Twelve of them added 479 kW to a datacenter whose real
# end load is about 145 kW.
LOAD_TYPES = [
    *IT_TYPES,
    "crah", "cdu", "chiller", "cooling_tower", "pump", "valve",
    "oob_switch", "bacnet_router", "modbus_gateway",
]

UNKNOWN = "unknown"
MEASURED = "measured"
DERIVED = "derived"
INFERRED = "inferred"
ASSUMED = "assumed"


@dataclass
class Constraint:
    name: str
    unit: str
    used_p95: float | None = None
    used_peak: float | None = None
    capacity: float | None = None
    # Where the capacity figure came from, because "measured from the plant"
    # and "assumed because nobody recorded it" are different confidences.
    capacity_source: str = UNKNOWN
    note: str | None = None

    @property
    def known(self) -> bool:
        return self.capacity is not None and self.capacity > 0

    @property
    def headroom(self) -> float | None:
        if not self.known or self.used_p95 is None:
            return None
        return self.capacity - self.used_p95

    @property
    def utilisation_pct(self) -> float | None:
        if not self.known or self.used_p95 is None:
            return None
        return self.used_p95 / self.capacity * 100.0

    @property
    def tight(self) -> bool:
        u = self.utilisation_pct
        return u is not None and u >= TIGHT_PCT


def binding(constraints: list[Constraint]) -> tuple[Constraint | None, str]:
    """Which constraint runs out first.

    Only constraints with a known limit can bind. One with no rating is not
    "fine" - it is unmeasured, and saying so is the difference between a
    capacity report and a capacity guess.
    """
    known = [c for c in constraints if c.known and c.utilisation_pct is not None]
    unknown = [c.name for c in constraints if not c.known]

    if not known:
        return None, (
            "no constraint has a known limit"
            + (f" ({', '.join(unknown)} unrated)" if unknown else ""))

    worst = max(known, key=lambda c: c.utilisation_pct or 0.0)
    reason = (f"{worst.name} at {worst.utilisation_pct:.0f}% "
              f"({worst.used_p95:.1f} of {worst.capacity:.1f} {worst.unit})")
    if unknown:
        reason += (f"; {', '.join(unknown)} could not be judged - no rating "
                   f"recorded, so something else may bind first")
    return worst, reason


@dataclass
class CapacityReport:
    scope: str
    scope_id: str
    name: str | None = None
    window_hours: float = 0.0
    constraints: list[Constraint] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        worst, reason = binding(self.constraints)
        return {
            "scope": self.scope,
            "scope_id": self.scope_id,
            "name": self.name,
            "percentile": PERCENTILE,
            "window_hours": round(self.window_hours, 1),
            "binding_constraint": worst.name if worst else None,
            "binding_reason": reason,
            "constraints": [
                {
                    "name": c.name, "unit": c.unit,
                    "used_p95": None if c.used_p95 is None else round(c.used_p95, 2),
                    "used_peak": None if c.used_peak is None else round(c.used_peak, 2),
                    "capacity": None if c.capacity is None else round(c.capacity, 2),
                    "capacity_source": c.capacity_source,
                    "headroom": None if c.headroom is None else round(c.headroom, 2),
                    "utilisation_pct": (None if c.utilisation_pct is None
                                        else round(c.utilisation_pct, 1)),
                    "tight": c.tight,
                    "note": c.note,
                }
                for c in self.constraints
            ],
            "notes": self.notes,
        }


async def report(session, *, scope: str, scope_id: str, hours: int = 720,
                 assumed_rack_kw: float | None = None) -> dict[str, Any]:
    """Build the four-constraint report for one scope.

    ``assumed_rack_kw`` lets a caller supply the design figure that inventory is
    missing. It is applied as an assumption and labelled as one, so the number
    is usable without the report pretending to know something it does not.
    """
    from app.repositories import capacity as repo

    name = await repo.scope_name(session, scope=scope, scope_id=scope_id)
    mid = await repo.metric_id(session, "power_draw")
    device_ids = await repo.devices_in_scope(session, scope=scope,
                                             scope_id=scope_id,
                                             device_types=LOAD_TYPES)
    it_ids = await repo.devices_in_scope(session, scope=scope,
                                         scope_id=scope_id,
                                         device_types=IT_TYPES)
    power = await repo.coincident_power(session, device_ids=device_ids,
                                        power_metric_id=mid, hours=hours,
                                        percentile=PERCENTILE)
    it_power = await repo.coincident_power(session, device_ids=it_ids,
                                           power_metric_id=mid, hours=hours,
                                           percentile=PERCENTILE)
    sp = await repo.space(session, scope=scope, scope_id=scope_id)
    cool = await repo.cooling_capacity(session, scope=scope, scope_id=scope_id)
    prt = await repo.ports(session, scope=scope, scope_id=scope_id)

    buckets = int(power.get("buckets") or 0)
    rep = CapacityReport(scope=scope, scope_id=scope_id, name=name,
                         window_hours=buckets / 60.0)

    # --- power ---------------------------------------------------------------
    p95_kw = (power.get("p95_w") or 0) / 1000.0 if power.get("p95_w") else None
    peak_kw = (power.get("peak_w") or 0) / 1000.0 if power.get("peak_w") else None
    racks = int(sp.get("racks") or 0)
    capacity_kw = None
    source = UNKNOWN
    note = ("no rack, PDU or RPP in this scope carries a power rating, so there "
            "is nothing to measure the load against")
    if assumed_rack_kw and racks:
        capacity_kw = assumed_rack_kw * racks
        source = ASSUMED
        note = (f"assumed {assumed_rack_kw:g} kW per rack across {racks} racks - "
                f"supplied by the caller, not recorded in inventory")
    rep.constraints.append(Constraint(
        name="power", unit="kW", used_p95=p95_kw, used_peak=peak_kw,
        capacity=capacity_kw, capacity_source=source, note=note))

    # --- cooling -------------------------------------------------------------
    cool_kw = float(cool.get("capacity_kw") or 0) or None
    rep.constraints.append(Constraint(
        name="cooling", unit="kW",
        # IT heat only. Essentially all the electrical power into IT equipment
        # leaves as heat, but the cooling plant's own draw is not a load on
        # itself.
        used_p95=(it_power.get("p95_w") or 0) / 1000.0 or None,
        used_peak=(it_power.get("peak_w") or 0) / 1000.0 or None,
        capacity=cool_kw,
        capacity_source=MEASURED if cool_kw else UNKNOWN,
        note=(f"{cool.get('units', 0)} chiller(s); heat load taken as the "
              f"electrical power drawn, essentially all of which leaves as heat"
              if cool_kw else
              "cooling capacity is only known at plant level here - the CRAH "
              "capacity point is a duty percentage, not kilowatts")))

    # --- space ---------------------------------------------------------------
    u_total = float(sp.get("u_total") or 0) or None
    u_used = float(sp.get("u_used") or 0)
    rep.constraints.append(Constraint(
        name="space", unit="U", used_p95=u_used, used_peak=u_used,
        capacity=u_total,
        capacity_source=MEASURED if u_total else UNKNOWN,
        note=(f"{racks} rack(s); zero-U gear excluded, it occupies no rail"
              if u_total else "no racks in this scope")))

    # --- ports ---------------------------------------------------------------
    total_ports = float(prt.get("total_ports") or 0) or None
    used_ports = float(prt.get("used_ports") or 0)
    rep.constraints.append(Constraint(
        name="ports", unit="ports", used_p95=used_ports, used_peak=used_ports,
        capacity=total_ports,
        capacity_source=INFERRED if total_ports else UNKNOWN,
        note=(f"{prt.get('switches', 0)} switch(es); usage inferred from "
              f"operational state, so a patched but idle port reads as free"
              if total_ports else "no switches in this scope")))

    if buckets == 0:
        rep.notes.append("no power telemetry in the window, so the load "
                         "percentile could not be computed")
    elif buckets < hours * 60 * 0.5:
        rep.notes.append(
            f"the {PERCENTILE}th percentile covers {buckets / 60.0:.1f} h of "
            f"data, not the {hours} h requested - the fleet has not been "
            f"recording that long")
    return rep.as_dict()
