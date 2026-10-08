"""The thermal indices a room's own sensors can support (docs/27 D6, Phase 2).

Three families, each published with its source so a reader knows what it was
scored from:

  * RCI, the Rack Cooling Index (Herrlin, ASHRAE Transactions 2005) - how far
    the intakes sit above (HI) or below (LO) the recommended band, as a share
    of the way to the allowable limit. 100 % means no intake outside the
    recommended band; 0 % means the intakes are, on average, at the allowable
    limit. Herrlin's rating: >= 96 good, 91-95 acceptable, <= 90 poor.

  * RTI, the Return Temperature Index (Herrlin 2007) - the air handlers' rise
    against the equipment's rise. 100 % is balanced; above it the return is
    warmer than the kit alone explains, which is exhaust RECIRCULATING into
    intakes; below it the return is cooler, which is supply BYPASSING the kit.

  * SHI / RHI, the Supply and Return Heat Indices (Sharma, Bash & Patel 2002)
    - of the heat the air picks up before it leaves a rack, how much it picked
    up BEFORE entering (SHI) versus inside (RHI = 1 - SHI). 0 means the intake
    breathed pure supply air.

What is NOT here: the Capture Index. It needs airflow tracing, which no
temperature sensor gives, and publishing it from temperatures would be a
number with a name and no meaning.

Weighting: RTI and SHI are defined on airflow-weighted temperatures. No
server here meters its airflow, so the weights come from power: the air a
server moves is P / (rho * cp * dT), so a power-weighted mean of dT is
sum(P) / sum(P / dT). A device with no power reading falls back to unit
weight, and the payload says how many did.

Every function here is pure and takes plain numbers, so the tests pin the
arithmetic without a database and the THERMAL page can use the same ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.ashrae import Envelope

#: Herrlin's RCI rating bands, percent.
RCI_GOOD = 96.0
RCI_ACCEPTABLE = 91.0


def rci(intakes: list[float], env: Envelope) -> tuple[float | None, float | None]:
    """(RCI_HI, RCI_LO) in percent over every intake reading, or (None, None)
    with no intakes. Clamped at 0: an intake past the allowable limit cannot
    make the room MORE than wholly out of band."""
    n = len(intakes)
    if n == 0:
        return None, None
    span_hi = env.allow_high_c - env.rec_high_c
    span_lo = env.rec_low_c - env.allow_low_c
    over = sum(t - env.rec_high_c for t in intakes if t > env.rec_high_c)
    under = sum(env.rec_low_c - t for t in intakes if t < env.rec_low_c)
    hi = 100.0 * (1.0 - over / (span_hi * n)) if span_hi > 0 else None
    lo = 100.0 * (1.0 - under / (span_lo * n)) if span_lo > 0 else None
    clamp = lambda v: None if v is None else max(0.0, min(100.0, v))  # noqa: E731
    return clamp(hi), clamp(lo)


def rci_rating(value: float | None) -> str | None:
    if value is None:
        return None
    if value >= RCI_GOOD:
        return "good"
    if value >= RCI_ACCEPTABLE:
        return "acceptable"
    return "poor"


def power_weighted_dt(pairs: list[tuple[float, float | None]]) -> tuple[float | None, int]:
    """Airflow-weighted equipment rise from (dT, power_w) pairs.

    Returns (dT_equip, unweighted) where `unweighted` counts the devices that
    had no power reading and so entered with unit weight. A non-positive dT is
    dropped: a server whose exhaust reads cooler than its inlet is a sensor
    fault, not negative heat.
    """
    good = [(dt, p) for dt, p in pairs if dt is not None and dt > 0]
    if not good:
        return None, 0
    weighted = [(dt, p) for dt, p in good if p is not None and p > 0]
    unweighted = len(good) - len(weighted)
    if weighted and not unweighted:
        return sum(p for _, p in weighted) / sum(p / dt for dt, p in weighted), 0
    # Mixed or no power: the honest answer is the plain mean, flagged.
    return sum(dt for dt, _ in good) / len(good), unweighted


def rti(supply_c: float | None, return_c: float | None,
        dt_equip: float | None) -> float | None:
    """Return Temperature Index, percent. None when a leg is missing or the
    equipment rise is not positive."""
    if supply_c is None or return_c is None or dt_equip is None or dt_equip <= 0:
        return None
    return 100.0 * (return_c - supply_c) / dt_equip


def shi(inlets: list[tuple[float, float]], supply_ref_c: float | None) -> float | None:
    """Supply Heat Index over (inlet_c, exhaust_c) pairs against the reference
    supply. sum(T_in - T_ref) / sum(T_out - T_ref). None without a reference
    or without a positive total rise. Clamped to [0, 1]: an inlet below the
    reference supply is measurement spread, not negative recirculation."""
    if supply_ref_c is None or not inlets:
        return None
    num = sum(t_in - supply_ref_c for t_in, _ in inlets)
    den = sum(t_out - supply_ref_c for _, t_out in inlets)
    if den <= 0:
        return None
    return max(0.0, min(1.0, num / den))


# ------------------------------------------------------------- room roll-up

@dataclass
class RackThermal:
    """One rack's intake and exhaust readings, already attributed."""

    rack_id: str
    inlets_c: list[float] = field(default_factory=list)
    exhausts_c: list[float] = field(default_factory=list)
    #: (dT, power_w) per device that reported both an inlet and an exhaust.
    device_rises: list[tuple[float, float | None]] = field(default_factory=list)


@dataclass
class RackIndex:
    rack_id: str
    inlet_max_c: float | None
    inlet_min_c: float | None
    exhaust_max_c: float | None
    #: Spread of the intake readings across the rack, K. What the reference
    #: viewer calls temperature variance.
    spread_k: float | None
    #: Mean exhaust minus mean inlet, K.
    rise_k: float | None
    shi: float | None
    rhi: float | None
    #: 'allowable' above the recommended ceiling, 'out' above the allowable
    #: one, None otherwise. A hot spot is a rack with either.
    hot: str | None
    intakes: int
    exhausts: int


@dataclass
class RoomIndices:
    ashrae_class: str
    rci_hi: float | None
    rci_lo: float | None
    rci_rating: str | None
    rti: float | None
    shi: float | None
    rhi: float | None
    supply_ref_c: float | None
    return_ref_c: float | None
    dt_equip_k: float | None
    intakes: int
    exhausts: int
    #: Devices whose rise entered RTI with unit weight for want of power.
    unweighted: int
    units_in_ref: int
    hot_spots: int
    racks: list[RackIndex] = field(default_factory=list)


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def rack_index(r: RackThermal, env: Envelope, supply_ref_c: float | None) -> RackIndex:
    i_max = max(r.inlets_c) if r.inlets_c else None
    i_min = min(r.inlets_c) if r.inlets_c else None
    e_max = max(r.exhausts_c) if r.exhausts_c else None
    i_mean, e_mean = _mean(r.inlets_c), _mean(r.exhausts_c)
    rise = e_mean - i_mean if i_mean is not None and e_mean is not None else None
    s = shi([(i_mean, e_mean)], supply_ref_c) if i_mean is not None and e_mean is not None else None
    hot = None
    if i_max is not None:
        if i_max > env.allow_high_c:
            hot = "out"
        elif i_max > env.rec_high_c:
            hot = "allowable"
    return RackIndex(
        rack_id=r.rack_id, inlet_max_c=i_max, inlet_min_c=i_min, exhaust_max_c=e_max,
        spread_k=(i_max - i_min) if i_max is not None and i_min is not None else None,
        rise_k=rise, shi=s, rhi=(1.0 - s) if s is not None else None, hot=hot,
        intakes=len(r.inlets_c), exhausts=len(r.exhausts_c),
    )


def room_indices(racks: list[RackThermal], env: Envelope,
                 units: list[tuple[float | None, float | None]]) -> RoomIndices:
    """Everything the room and rack panels show, from attributed readings and
    the air handlers' (supply_c, return_c) pairs. A unit missing either leg is
    left out of the reference; the count of units that made it is published."""
    pairs = [(s, r) for s, r in units if s is not None and r is not None]
    supply_ref = _mean([s for s, _ in pairs])
    return_ref = _mean([r for _, r in pairs])

    intakes = [t for r in racks for t in r.inlets_c]
    exhausts = [t for r in racks for t in r.exhausts_c]
    hi, lo = rci(intakes, env)
    dt_equip, unweighted = power_weighted_dt([d for r in racks for d in r.device_rises])

    per_rack = [rack_index(r, env, supply_ref) for r in racks]
    rack_pairs = [(_mean(r.inlets_c), _mean(r.exhausts_c)) for r in racks
                  if r.inlets_c and r.exhausts_c]
    room_shi = shi([(a, b) for a, b in rack_pairs if a is not None and b is not None], supply_ref)
    return RoomIndices(
        ashrae_class=env.name, rci_hi=hi, rci_lo=lo, rci_rating=rci_rating(hi),
        rti=rti(supply_ref, return_ref, dt_equip),
        shi=room_shi, rhi=(1.0 - room_shi) if room_shi is not None else None,
        supply_ref_c=supply_ref, return_ref_c=return_ref, dt_equip_k=dt_equip,
        intakes=len(intakes), exhausts=len(exhausts), unweighted=unweighted,
        units_in_ref=len(pairs), hot_spots=sum(1 for r in per_rack if r.hot),
        racks=per_rack,
    )
