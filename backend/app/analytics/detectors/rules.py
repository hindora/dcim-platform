"""L2: state-gated rules, and the dwell framework every detector runs on.

ASHRAE Guideline 36 section 5.16 is the model: an automated fault-detection
rule names its **required points** (missing any one disables it on that
unit - analytics/catalogue.missing_points), an **operating-state gate** (a
stopped unit has no "supply over setpoint"), a **tolerance** with hysteresis,
and a **set / clear dwell** so a transient never alarms and an alarm never
flaps. That is how BMS fault detection ships in production (Siemens Desigo
"fault rules", Schneider EBO FDD, Johnson Controls' G36 sequences), and it is
deliberately boring: every alert here can be explained to a technician in one
sentence with the numbers that tripped it.

Three outcomes of evaluating a rule on one series this sweep (`Verdict.cond`):

  True   the condition holds       pending -> (dwell) -> active, or touch
  False  it does not, or a GATE    pending resets; an active finding counts
         closed (unit stopped,     toward its clear dwell. A stopped CRAH is
         under maintenance, gas    not over setpoint: G36 suppresses the fault
         pre-discharge)            when the operating state does not apply.
  None   cannot tell: a required   HOLD. Neither the set nor the clear dwell
         input is MASKED by L0 or  advances. A sensor gone bad must not open
         missing this sweep        a process alert, and must not clear one.

"Standby" needs no gate of its own here: the estate has no standby point, and
a standby unit is a healthy STOPPED unit (services/plant.py), which the
running gate already excludes.

Dwell is time (`dwell_s` / `clear_dwell_s`, measured on the sweep clock) or
samples (`dwell_samples` / `clear_samples`, counted only on sweeps where a
new sample arrived), per docs/29 section 12.1. The state is the compact
DwellState kept in the `dcim:anomaly:acc` hash.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.thermal import percentile

Key = tuple[str, str]

#: How far back a last-known value may be read: twice the boolean heartbeat
#: (ingest/changelog.py LAST_KNOWN_WINDOW_S). A heartbeat row is the same
#: value again, not a change.
LAST_KNOWN_S = 1800

#: A value older than this is not "the current reading" for a rule.
MAX_VALUE_AGE_S = 900


# --- the device view --------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Sample:
    ts: float          # epoch seconds
    v: float
    q: str = "good"    # good | suspect | bad (ingest quality names)


@dataclass
class BoolSeries:
    """A change-logged boolean, rebuilt as a last-known value.

    `since` is when the current value began. `since_known` says whether that
    is an observed transition or just the oldest row in the look-back (a
    heartbeat row is not a start). `starts` are observed 0 -> 1 transitions.
    """

    value: bool | None = None
    last_ts: float | None = None
    since: float | None = None
    since_known: bool = False
    starts: list[float] = field(default_factory=list)

    def known(self, now: float, lookback_s: float = LAST_KNOWN_S) -> bool | None:
        if self.value is None or self.last_ts is None or now - self.last_ts > lookback_s:
            return None
        return self.value


@dataclass
class DeviceView:
    device_id: str
    name: str
    device_type: str
    room_id: str | None = None
    datacenter_id: str | None = None
    lifecycle: str = "in_service"
    num: dict[Key, list[Sample]] = field(default_factory=dict)
    bools: dict[Key, BoolSeries] = field(default_factory=dict)
    #: Series an L0 check condemns right now: rules HOLD on them.
    masked: set[Key] = field(default_factory=set)
    #: Individual samples L0 removed (a masked spike, an impossible value).
    dropped: dict[Key, set[float]] = field(default_factory=dict)
    #: Series that received a new sample this sweep.
    fresh: set[Key] = field(default_factory=set)
    online: bool = True
    poll_interval_s: int | None = None
    maintenance: bool = False
    fire_predischarge: bool = False

    def pick(self, key: str, inst: str | None = None) -> Key:
        """The series key: exact instance, or '' / the only instance."""
        if inst is not None:
            return (key, inst)
        if (key, "") in self.num or (key, "") in self.bools:
            return (key, "")
        found = sorted(k for k in (*self.num, *self.bools) if k[0] == key)
        return found[0] if found else (key, "")

    def clean(self, key: Key, since: float | None = None) -> list[Sample]:
        """GOOD samples L0 did not drop, oldest first."""
        gone = self.dropped.get(key, ())
        out = [s for s in self.num.get(key, ()) if s.q == "good" and s.ts not in gone]
        if since is not None:
            out = [s for s in out if s.ts >= since]
        return out

    def last(self, key: Key, now: float, max_age_s: float = MAX_VALUE_AGE_S) -> float | None:
        s = self.clean(key)
        if not s or now - s[-1].ts > max_age_s:
            return None
        return s[-1].v

    def instances(self, key: str) -> list[Key]:
        return sorted(k for k in self.num if k[0] == key)

    def available(self) -> list[Key]:
        return [*self.num, *self.bools]


@dataclass
class Context:
    """What a rule needs from beyond its own device: the room."""

    #: room id -> latest clean inlet readings of every rack sensor / PDU probe.
    room_inlet: dict[str, list[float]] = field(default_factory=dict)
    #: room id -> the highest dew point any room sensor implies.
    room_dew_max: dict[str, float] = field(default_factory=dict)


# --- verdicts and the dwell state machine -----------------------------------

@dataclass
class Verdict:
    """One series' answer this sweep."""

    metric_key: str
    instance: str
    cond: bool | None
    gate: str | None = None
    observed: float | None = None
    expected: float | None = None
    threshold: float | None = None
    score: float | None = None
    diagnosis: str | None = None
    expr: str = ""
    summary: str = ""
    #: The series the rule read, for the evidence's signals[] and masks[].
    signals: tuple[Key, ...] = ()
    #: metric_key to pass raise_alarm: the primary NUMERIC metric, or None.
    #: Never equipment_state / alarm_state, or detection_for files the derived
    #: alert as `state` (alert_taxonomy.detection_for; docs/29 D14).
    alarm_metric_key: str | None = None
    #: A new sample arrived for the primary series (sample-count dwells).
    fresh: bool = True
    #: Detector-private state to persist (PU-3's mean flow, GN-1's anchors).
    x: dict[str, Any] | None = None


@dataclass
class DwellState:
    active: bool = False
    pending_since: float | None = None
    clear_since: float | None = None
    n_true: int = 0
    n_false: int = 0
    first_seen: float | None = None
    reopens: int = 0
    x: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"a": int(self.active)}
        for k, v in (("p", self.pending_since), ("c", self.clear_since),
                     ("o", self.first_seen)):
            if v is not None:
                out[k] = round(v, 3)
        if self.n_true:
            out["nt"] = self.n_true
        if self.n_false:
            out["nf"] = self.n_false
        if self.reopens:
            out["r"] = self.reopens
        if self.x:
            out["x"] = self.x
        return out

    @classmethod
    def from_wire(cls, d: Mapping[str, Any] | None) -> DwellState:
        if not d:
            return cls()
        return cls(active=bool(d.get("a")), pending_since=d.get("p"),
                   clear_since=d.get("c"), n_true=int(d.get("nt", 0)),
                   n_false=int(d.get("nf", 0)), first_seen=d.get("o"),
                   reopens=int(d.get("r", 0)), x=dict(d.get("x") or {}))


OPEN, TOUCH, RESOLVE = "open", "touch", "resolve"

#: More reopens than this inside the window hold a chattering finding open
#: until the condition has been clear for a whole reopen window (docs/29 8.2).
MAX_REOPENS = 3


def _set_done(st: DwellState, p: Mapping[str, Any], now: float) -> bool:
    if p.get("dwell_samples"):
        return st.n_true >= int(p["dwell_samples"])
    return now - (st.pending_since or now) >= float(p.get("dwell_s", 0))


def _clear_done(st: DwellState, p: Mapping[str, Any], now: float) -> bool:
    wait = float(p.get("clear_dwell_s", 0))
    if st.reopens > MAX_REOPENS:
        wait = max(wait, float(p.get("reopen_window_s", 0)))
        return now - (st.clear_since or now) >= wait
    if p.get("clear_samples"):
        return st.n_false >= int(p["clear_samples"])
    return now - (st.clear_since or now) >= wait


def step(st: DwellState, v: Verdict, params: Mapping[str, Any], now: float) -> str | None:
    """Advance one series' dwell state. Returns OPEN, TOUCH, RESOLVE or None.

    TOUCH means "active and still true" - the finding's last_seen moves. A
    finding that is clearing, or held under a mask, is not touched: last_seen
    is the last sweep the condition was seen to hold.
    """
    if v.x is not None:
        st.x = v.x
    if v.cond is None and v.gate is None:
        return None                       # hold under a mask / missing input
    if v.cond is True and v.gate is None:
        st.clear_since, st.n_false = None, 0
        if st.active:
            return TOUCH
        if st.pending_since is None:
            st.pending_since, st.n_true = now, 0
        if v.fresh:
            st.n_true += 1
        if _set_done(st, params, now):
            st.active, st.first_seen = True, st.pending_since
            st.pending_since, st.n_true = None, 0
            return OPEN
        return None
    # false, or gated
    st.pending_since, st.n_true = None, 0
    if not st.active:
        return None
    if st.clear_since is None:
        st.clear_since, st.n_false = now, 0
    if v.fresh or v.gate is not None:
        st.n_false += 1
    if _clear_done(st, params, now):
        st.active, st.clear_since, st.n_false = False, None, 0
        return RESOLVE
    return None


# --- small numerics -----------------------------------------------------------

def slope_per_s(samples: list[Sample]) -> float | None:
    """Least-squares slope on real timestamps (polls are not evenly spaced)."""
    n = len(samples)
    if n < 3:
        return None
    t0 = samples[0].ts
    xs = [s.ts - t0 for s in samples]
    ys = [s.v for s in samples]
    xm, ym = sum(xs) / n, sum(ys) / n
    sxx = sum((x - xm) ** 2 for x in xs)
    if sxx <= 0:
        return None
    return sum((x - xm) * (y - ym) for x, y in zip(xs, ys, strict=True)) / sxx


def reversals(values: list[float], min_step: float) -> int:
    """Direction changes of at least `min_step`, zig-zag style.

    The running extreme is tracked in the current direction; a move of
    `min_step` back from it is a reversal. Movement inside the step is a
    positioner's deadband, not hunting.
    """
    if len(values) < 3:
        return 0
    direction = 0
    extreme = values[0]
    count = 0
    for v in values[1:]:
        if direction >= 0 and v >= extreme:
            extreme = v
            if direction == 0 and v - values[0] >= min_step:
                direction = 1
        elif direction <= 0 and v <= extreme:
            extreme = v
            if direction == 0 and values[0] - v >= min_step:
                direction = -1
        if direction == 1 and extreme - v >= min_step:
            count += 1
            direction, extreme = -1, v
        elif direction == -1 and v - extreme >= min_step:
            count += 1
            direction, extreme = 1, v
    return count


def room_p90(values: list[float]) -> float | None:
    """Nearest-rank p90, the same function the room page uses (parity)."""
    return percentile(values, 90)


# --- gates ----------------------------------------------------------------------

def context_gate(p: Mapping[str, Any], dev: DeviceView) -> str | None:
    if p.get("exclude_maintenance", True) and dev.maintenance:
        return "maintenance"
    if p.get("exclude_fire_predischarge", False) and dev.fire_predischarge:
        return "fire_predischarge"
    return None


def running_gate(dev: DeviceView, inst: str, now: float, *, running_min_s: float = 0,
                 restart_exclusion_s: float = 0) -> tuple[str | None, bool]:
    """(gate, unknown) for a unit's run proof `equipment_state{inst}`."""
    b = dev.bools.get(("equipment_state", inst))
    val = b.known(now) if b else None
    if b is None or val is None:
        return None, True
    if not val:
        return "not_running", False
    run_for = now - b.since if b.since is not None else 0.0
    if restart_exclusion_s and b.since_known and run_for < restart_exclusion_s:
        return "restart_exclusion", False
    if running_min_s and run_for < running_min_s:
        return "running_min", False
    return None, False


# --- the rules ----------------------------------------------------------------------

@dataclass
class RuleInput:
    detector_id: str
    params: Mapping[str, Any]
    dev: DeviceView
    ctx: Context
    now: float
    #: Is the finding on this (metric, instance) currently active?
    active: Callable[[Key], bool] = lambda k: False
    #: Detector-private persisted state for this (metric, instance).
    x: Callable[[Key], dict[str, Any]] = lambda k: {}


def _f(v: float | None, nd: int = 1) -> str:
    return "?" if v is None else f"{v:.{nd}f}"


def _held(key: Key, signals: tuple[Key, ...] = (), **kw) -> Verdict:
    return Verdict(key[0], key[1], None, signals=signals, **kw)


def _gated(key: Key, gate: str, signals: tuple[Key, ...] = (), **kw) -> Verdict:
    return Verdict(key[0], key[1], False, gate=gate, signals=signals, **kw)


def _masked(dev: DeviceView, keys: tuple[Key, ...]) -> bool:
    return any(k in dev.masked for k in keys)


def cr1(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    sup_k, sp_k = dev.pick("supply_air_temp"), dev.pick("air_setpoint_temp")
    valve_k = ("valve_position_pct", "CHW")
    sig = (sup_k, sp_k, valve_k, ("equipment_state", "Unit_Running"))
    kw = {"signals": sig, "alarm_metric_key": "supply_air_temp",
          "fresh": sup_k in dev.fresh}
    gate = context_gate(p, dev)
    run_gate, unknown = running_gate(dev, "Unit_Running", now,
                                     running_min_s=p.get("running_min_s", 600))
    if gate or run_gate:
        return [_gated(sup_k, gate or run_gate, **kw)]
    sup, sp = dev.last(sup_k, now), dev.last(sp_k, now)
    if unknown or _masked(dev, (sup_k, sp_k)) or sup is None or sp is None:
        return [_held(sup_k, **kw)]
    valve = None if valve_k in dev.masked else dev.last(valve_k, now)
    margin = p.get("clear_margin_k", 2.0) if ri.active(sup_k) else p.get("margin_k", 3.0)
    over = sup - sp
    if valve is None:
        diag = "undetermined"
    elif valve >= p.get("valve_starved_pct", 95.0):
        diag = "chw_starved"
    else:
        diag = "control_fault"
    why = {"chw_starved": f"valve {_f(valve, 0)}% open - chilled water starved",
           "control_fault": f"valve only {_f(valve, 0)}% open - a control fault",
           "undetermined": "valve position unknown"}[diag]
    return [Verdict(
        sup_k[0], sup_k[1], over > margin, observed=sup, expected=sp,
        threshold=sp + margin, score=over, diagnosis=diag,
        expr=(f"supply_air_temp - air_setpoint_temp > {margin}"
              f" (valve_position_pct{{CHW}} >= {p.get('valve_starved_pct', 95.0)}"
              " -> chw_starved)"),
        summary=(f"{dev.name} discharging {_f(sup)} C against a {_f(sp)} C setpoint; "
                 f"{why}"), **kw)]


def cr2(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    ret_k, sup_k, fan_k = dev.pick("return_air_temp"), dev.pick("supply_air_temp"), \
        dev.pick("fan_speed_pct")
    kw = {"signals": (ret_k, sup_k, fan_k, ("equipment_state", "Unit_Running")),
          "alarm_metric_key": "return_air_temp", "fresh": ret_k in dev.fresh}
    gate = context_gate(p, dev)
    run_gate, unknown = running_gate(dev, "Unit_Running", now,
                                     running_min_s=p.get("running_min_s", 600))
    if gate or run_gate:
        return [_gated(ret_k, gate or run_gate, **kw)]
    ret, sup, fan = dev.last(ret_k, now), dev.last(sup_k, now), dev.last(fan_k, now)
    if unknown or _masked(dev, (ret_k, sup_k, fan_k)) or None in (ret, sup, fan):
        return [_held(ret_k, **kw)]
    if fan < p.get("min_fan_pct", 30.0):
        return [_gated(ret_k, "low_fan", **kw)]
    limit = p.get("clear_delta_k", 5.0) if ri.active(ret_k) else p.get("min_delta_k", 4.0)
    dt = ret - sup
    return [Verdict(
        ret_k[0], ret_k[1], dt < limit, observed=dt, expected=limit, threshold=limit,
        score=limit - dt, diagnosis="low_delta_t",
        expr=f"return_air_temp - supply_air_temp < {limit} AND fan_speed_pct >= "
             f"{p.get('min_fan_pct', 30.0)}",
        summary=(f"{dev.name} returning {_f(ret)} C over a {_f(sup)} C supply: a "
                 f"{_f(dt)} K delta-T at {_f(fan, 0)}% fan - bypass air or "
                 f"over-provisioned"), **kw)]


def _hunting(ri: RuleInput, key: Key, *, run_inst: str | None) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    sig = (key,) + ((("equipment_state", run_inst),) if run_inst else ())
    kw = {"signals": sig, "alarm_metric_key": "valve_position_pct",
          "fresh": key in dev.fresh}
    gate = context_gate(p, dev)
    unknown = False
    if run_inst:
        run_gate, unknown = running_gate(dev, run_inst, now,
                                         running_min_s=p.get("running_min_s", 0))
        gate = gate or run_gate
    if gate:
        return [_gated(key, gate, **kw)]
    window = float(p.get("window_s", 1800))
    samples = dev.clean(key, since=now - window)
    if unknown or key in dev.masked or len(samples) < max(3, int(p.get("min_samples", 3))):
        return [_held(key, **kw)]
    n = reversals([s.v for s in samples], float(p.get("min_step_pct", 5.0)))
    need = int(p.get("min_reversals", 6))
    return [Verdict(
        key[0], key[1], n >= need, observed=float(n), expected=None, threshold=float(need),
        score=n / need if need else None, diagnosis="hunting",
        expr=f"reversals(valve_position_pct{{{key[1]}}}, >= {p.get('min_step_pct', 5.0)} pp,"
             f" {int(window)} s) >= {need}",
        summary=(f"{dev.name} valve reversed {n} times in {int(window // 60)} min - "
                 f"hunting"), **kw)]


def cr6(ri: RuleInput) -> list[Verdict]:
    return _hunting(ri, ("valve_position_pct", "CHW"), run_inst="Unit_Running")


def va2(ri: RuleInput) -> list[Verdict]:
    return _hunting(ri, ("valve_position_pct", "MEASURED"), run_inst=None)


def ch1(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    sup_k, sp_k = ("water_supply_temp", "CHW"), ("water_setpoint_temp", "CHW")
    load_k = dev.pick("compressor_load_pct")
    kw = {"signals": (sup_k, sp_k, load_k, ("equipment_state", "Chiller_Running")),
          "alarm_metric_key": "water_supply_temp", "fresh": sup_k in dev.fresh}
    gate = context_gate(p, dev)
    run_gate, unknown = running_gate(
        dev, "Chiller_Running", now, running_min_s=p.get("running_min_s", 900),
        restart_exclusion_s=p.get("restart_exclusion_s", 480))
    if gate or run_gate:
        return [_gated(sup_k, gate or run_gate, **kw)]
    sup, sp, load = dev.last(sup_k, now), dev.last(sp_k, now), dev.last(load_k, now)
    if unknown or _masked(dev, (sup_k, sp_k, load_k)) or None in (sup, sp, load):
        return [_held(sup_k, **kw)]
    if load >= p.get("max_load_pct", 95.0):
        return [_gated(sup_k, "at_capacity", **kw)]
    margin = p.get("clear_margin_k", 0.5) if ri.active(sup_k) else p.get("margin_k", 1.0)
    over = sup - sp
    return [Verdict(
        sup_k[0], sup_k[1], over > margin, observed=sup, expected=sp,
        threshold=sp + margin, score=over, diagnosis="chw_over_setpoint",
        expr=f"water_supply_temp{{CHW}} - water_setpoint_temp{{CHW}} > {margin} AND "
             f"compressor_load_pct < {p.get('max_load_pct', 95.0)}",
        summary=(f"{dev.name} leaving {_f(sup)} C chilled water against a {_f(sp)} C "
                 f"setpoint at {_f(load, 0)}% load"), **kw)]


def ch4(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    key = ("equipment_state", "Chiller_Running")
    # metric_key=None on the alarm: this rule reads a STATE key, and passing
    # it would file the derived alert as detection 'state' (docs/29 D14).
    kw = {"signals": (key,), "alarm_metric_key": None}
    b = dev.bools.get(key)
    gate = context_gate(p, dev)
    if gate:
        return [_gated(key, gate, **kw)]
    if b is None or (b.known(now, p.get("lookback_s", LAST_KNOWN_S)) is None and not b.starts):
        return [_held(key, **kw)]
    window = float(p.get("window_s", 3600))
    starts = [t for t in b.starts if now - t <= window]
    limit = int(p.get("max_starts", 3))
    return [Verdict(
        key[0], key[1], len(starts) > limit, observed=float(len(starts)),
        threshold=float(limit), score=len(starts) / limit if limit else None,
        diagnosis="short_cycling",
        expr=f"starts(equipment_state{{Chiller_Running}}, {int(window)} s) > {limit}",
        summary=(f"{dev.name} started {len(starts)} times in the last "
                 f"{int(window // 60)} min"), **kw)]


def ch5(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    key, pres_k = ("water_return_temp", "COND"), ("water_pressure", "COND")
    kw = {"signals": (key, pres_k, ("equipment_state", "Chiller_Running")),
          "alarm_metric_key": "water_return_temp", "fresh": key in dev.fresh}
    gate = context_gate(p, dev)
    run_gate, unknown = running_gate(dev, "Chiller_Running", now)
    if gate or run_gate:
        return [_gated(key, gate or run_gate, **kw)]
    samples = dev.clean(key, since=now - 600)
    if unknown or key in dev.masked or len(samples) < 3:
        return [_held(key, **kw)]
    s = slope_per_s(samples)
    if s is None:
        return [_held(key, **kw)]
    last = samples[-1].v
    horizon = float(p.get("horizon_s", 600))
    trip = float(p.get("trip_c", 42.0))
    projected = last + s * horizon
    rise = s * 60.0
    return [Verdict(
        key[0], key[1], rise > p.get("rise_k_per_min", 0.3) and projected >= trip,
        observed=last, expected=None, threshold=trip, score=rise,
        diagnosis="condenser_rising",
        expr=f"slope(water_return_temp{{COND}}) > {p.get('rise_k_per_min', 0.3)} K/min AND "
             f"projected({int(horizon)} s) >= {trip}",
        summary=(f"{dev.name} condenser water at {_f(last)} C, rising {_f(rise, 2)} K/min "
                 f"- {_f(projected)} C projected in {int(horizon // 60)} min against a "
                 f"{_f(trip)} C trip"), **kw)]


def pl1(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    ret_k, sup_k, flow_k = ("water_return_temp", "CHW"), ("water_supply_temp", "CHW"), \
        ("water_flow", "CHW")
    kw = {"signals": (ret_k, sup_k, flow_k), "alarm_metric_key": "water_return_temp",
          "fresh": ret_k in dev.fresh}
    gate = context_gate(p, dev)
    if ("equipment_state", "Chiller_Running") in dev.bools:
        run_gate, _ = running_gate(dev, "Chiller_Running", now)
        gate = gate or run_gate
    if gate:
        return [_gated(ret_k, gate, **kw)]
    ret, sup = dev.last(ret_k, now), dev.last(sup_k, now)
    flows = dev.clean(flow_k, since=now - float(p.get("window_s", 1800)))
    if _masked(dev, (ret_k, sup_k, flow_k)) or ret is None or sup is None:
        return [_held(ret_k, **kw)]
    limit = p.get("min_fraction", 0.6) * p.get("design_delta_k", 5.5)
    dt = ret - sup
    rising = (slope_per_s(flows) or 0.0) > 0
    cond = dt < limit and (rising or not p.get("flow_rising", True))
    return [Verdict(
        ret_k[0], ret_k[1], cond, observed=dt, expected=p.get("design_delta_k", 5.5),
        threshold=limit, score=limit - dt, diagnosis="low_delta_t",
        expr=f"water_return_temp{{CHW}} - water_supply_temp{{CHW}} < {round(limit, 2)}"
             + (" AND slope(water_flow{CHW}) > 0" if p.get("flow_rising", True) else ""),
        summary=f"{dev.name} chilled-water range {_f(dt)} K against a {limit:.1f} K floor",
        **kw)]


def cd1(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    sup_k, sp_k, valve_k = ("water_supply_temp", ""), ("water_setpoint_temp", ""), \
        ("valve_position_pct", "FACILITY_CHW")
    kw = {"signals": (sup_k, sp_k, valve_k, ("equipment_state", "Unit_Running")),
          "alarm_metric_key": "water_supply_temp", "fresh": sup_k in dev.fresh}
    gate = context_gate(p, dev)
    run_gate, unknown = running_gate(dev, "Unit_Running", now)
    if gate or run_gate:
        return [_gated(sup_k, gate or run_gate, **kw)]
    sup, sp, valve = dev.last(sup_k, now), dev.last(sp_k, now), dev.last(valve_k, now)
    if unknown or _masked(dev, (sup_k, sp_k, valve_k)) or None in (sup, sp, valve):
        return [_held(sup_k, **kw)]
    margin = p.get("over_setpoint_k", 2.0)
    cond = valve >= p.get("valve_pct", 95.0) and sup > sp + margin
    return [Verdict(
        sup_k[0], sup_k[1], cond, observed=sup, expected=sp, threshold=sp + margin,
        score=sup - sp, diagnosis="facility_starved",
        expr=f"valve_position_pct{{FACILITY_CHW}} >= {p.get('valve_pct', 95.0)} AND "
             f"water_supply_temp - water_setpoint_temp > {margin}",
        summary=(f"{dev.name} secondary supply {_f(sup)} C against a {_f(sp)} C setpoint "
                 f"with the facility valve {_f(valve, 0)}% open - the primary loop is "
                 f"short"), **kw)]


def cd6(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    sup_k = ("water_supply_temp", "")
    kw = {"signals": (sup_k, ("equipment_state", "Unit_Running")),
          "alarm_metric_key": "water_supply_temp", "fresh": sup_k in dev.fresh}
    gate = context_gate(p, dev)
    run_gate, unknown = running_gate(dev, "Unit_Running", now)
    if gate or run_gate:
        return [_gated(sup_k, gate or run_gate, **kw)]
    sup = dev.last(sup_k, now)
    dew = ri.ctx.room_dew_max.get(dev.room_id or "")
    if unknown or sup_k in dev.masked or sup is None or dew is None:
        return [_held(sup_k, **kw)]
    limit = p.get("clear_margin_k", 3.0) if ri.active(sup_k) else p.get("min_margin_k", 2.0)
    margin = sup - dew
    return [Verdict(
        sup_k[0], sup_k[1], margin < limit, observed=sup, expected=dew,
        threshold=dew + limit, score=limit - margin, diagnosis="condensation_risk",
        expr=f"water_supply_temp - room_max(dew_point) < {limit}",
        summary=(f"{dev.name} supplying {_f(sup)} C water into a room whose dew point "
                 f"reaches {_f(dew)} C - {_f(margin)} K of margin"), **kw)]


def va1(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    meas_k, cmd_k = ("valve_position_pct", "MEASURED"), ("valve_position_pct", "COMMANDED")
    kw = {"signals": (meas_k, cmd_k), "alarm_metric_key": "valve_position_pct",
          "fresh": meas_k in dev.fresh}
    gate = context_gate(p, dev)
    if gate:
        return [_gated(meas_k, gate, **kw)]
    meas, cmd = dev.last(meas_k, now), dev.last(cmd_k, now)
    if _masked(dev, (meas_k, cmd_k)) or meas is None or cmd is None:
        return [_held(meas_k, **kw)]
    # Settled: the command has not moved within settle_s. A positioner chasing
    # a new command is travelling, not stuck.
    recent = dev.clean(cmd_k, since=now - float(p.get("settle_s", 120)))
    if any(abs(s.v - cmd) > 1.0 for s in recent):
        return [_gated(meas_k, "settling", **kw)]
    limit = p.get("clear_pct", 5.0) if ri.active(meas_k) else p.get("deviation_pct", 10.0)
    gap = abs(cmd - meas)
    return [Verdict(
        meas_k[0], meas_k[1], gap > limit, observed=meas, expected=cmd, threshold=limit,
        score=gap, diagnosis="actuator",
        expr=f"|valve_position_pct{{COMMANDED}} - valve_position_pct{{MEASURED}}| > {limit}",
        summary=(f"{dev.name} commanded {_f(cmd, 0)}% and sitting at {_f(meas, 0)}% - the "
                 f"actuator is not obeying"), **kw)]


def pu3(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    flow_k, speed_k = dev.pick("water_flow"), dev.pick("pump_speed_pct")
    run_k = ("equipment_state", "Run_Status")
    x = dict(ri.x(flow_k))
    kw = {"signals": (flow_k, run_k, speed_k), "alarm_metric_key": "water_flow",
          "fresh": flow_k in dev.fresh or speed_k in dev.fresh}
    gate = context_gate(p, dev)
    if gate:
        return [_gated(flow_k, gate, x=x, **kw)]
    b = dev.bools.get(run_k)
    status = b.known(now) if b else None
    flow, speed = dev.last(flow_k, now), dev.last(speed_k, now)
    if status is None or _masked(dev, (flow_k, speed_k)) or flow is None or speed is None:
        return [_held(flow_k, x=x, **kw)]
    frac = float(p.get("min_flow_fraction", 0.05))
    mean_flow = x.get("mf")
    run_for = now - b.since if b and b.since is not None else 0.0
    diag, cond = None, False
    if not status and (speed > p.get("min_speed_pct", 10.0)
                       or (mean_flow and flow > frac * mean_flow)):
        diag, cond = "running_unproven", True
    elif status and run_for >= p.get("prove_s", 60) and mean_flow and flow < frac * mean_flow:
        diag, cond = "proven_no_flow", True
    if status and not cond and flow > 0:
        # The reference flow is learned only while the pump is proven AND
        # flowing, so a fault cannot drag its own yardstick down.
        x["mf"] = round(flow if mean_flow is None else mean_flow + 0.05 * (flow - mean_flow), 4)
    text = {"running_unproven": (f"{dev.name} reports stopped while running at "
                                 f"{_f(speed, 0)}% speed and {_f(flow, 2)} L/s"),
            "proven_no_flow": (f"{dev.name} proves running with {_f(flow, 2)} L/s - under "
                               f"{int(frac * 100)}% of its usual {_f(mean_flow, 2)} L/s")}
    return [Verdict(
        flow_k[0], flow_k[1], cond, observed=flow, expected=mean_flow,
        threshold=(frac * mean_flow) if mean_flow else None, score=speed,
        diagnosis=diag,
        expr=("equipment_state{Run_Status} disagrees with pump_speed_pct / water_flow "
              f"(speed > {p.get('min_speed_pct', 10.0)}%, flow fraction {frac})"),
        summary=text.get(diag or "", f"{dev.name} pump proof agrees with process"),
        x=x, **kw)]


def rm2(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    window = float(p.get("window_s", 900))
    limit = float(p.get("max_rate_k_per_h", 20.0))
    gate = context_gate(p, dev)
    out = []
    for key in dev.instances("inlet_temperature") + dev.instances("ambient_temperature"):
        kw = {"signals": (key,), "alarm_metric_key": key[0], "fresh": key in dev.fresh}
        if gate:
            out.append(_gated(key, gate, **kw))
            continue
        samples = dev.clean(key, since=now - window)
        s = slope_per_s(samples) if key not in dev.masked else None
        if s is None:
            out.append(_held(key, **kw))
            continue
        rate = s * 3600.0
        out.append(Verdict(
            key[0], key[1], abs(rate) > limit, observed=samples[-1].v, threshold=limit,
            score=rate, diagnosis="rising" if rate > 0 else "falling",
            expr=f"|slope({key[0]}, {int(window)} s)| > {limit} K/h",
            summary=(f"{dev.name} {key[0].replace('_', ' ')} moving {_f(rate)} K/h "
                     f"against ASHRAE's {_f(limit, 0)} K/h"), **kw))
    return out


def rm3(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    peers = ri.ctx.room_inlet.get(dev.room_id or "", [])
    gate = context_gate(p, dev)
    out = []
    for key in dev.instances("inlet_temperature"):
        kw = {"signals": (key,), "alarm_metric_key": "inlet_temperature",
              "fresh": key in dev.fresh}
        if gate:
            out.append(_gated(key, gate, **kw))
            continue
        x = dev.last(key, now)
        if key in dev.masked or x is None or len(peers) < int(p.get("min_room_sensors", 5)):
            out.append(_held(key, **kw))
            continue
        p90 = room_p90(peers)
        margin = p.get("clear_margin_k", 2.0) if ri.active(key) else p.get("margin_k", 3.0)
        out.append(Verdict(
            key[0], key[1], x > p90 + margin, observed=x, expected=p90,
            threshold=p90 + margin, score=x - p90, diagnosis="hot_spot",
            expr=f"inlet_temperature > room_p90(inlet_temperature) + {margin}",
            summary=(f"{dev.name} intake {_f(x)} C against a room p90 of {_f(p90)} C"),
            **kw))
    return out


def gn1(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    key, run_k = dev.pick("fuel_level_pct"), ("equipment_state", "Engine_Running")
    x = dict(ri.x(key))
    kw = {"signals": (key, run_k), "alarm_metric_key": "fuel_level_pct",
          "fresh": key in dev.fresh}
    b = dev.bools.get(run_k)
    running = b.known(now) if b else None
    level = dev.last(key, now)
    if running is None or key in dev.masked or level is None:
        return [_held(key, x=x, **kw)]
    if running:
        return [_gated(key, "running", x={}, **kw)]
    stopped_for = now - b.since if b.since is not None else 0.0
    if stopped_for < p.get("not_running_min_s", 1800):
        return [_gated(key, "recently_run", x={}, **kw)]
    window = float(p.get("window_s", 3600))
    # Anchors every 5 min across the window: the highest level seen in it is
    # the reference, so a rise (temperature, a polishing loop) never counts.
    anchors = [a for a in x.get("h", []) if now - a[0] <= window]
    if not anchors or now - anchors[-1][0] >= 300:
        anchors.append([round(now, 1), level])
    x["h"] = anchors
    ref = max(a[1] for a in anchors)
    span = now - anchors[0][0]
    drop = ref - level
    cond = span >= 0.5 * window and drop >= p.get("min_drop_pct", 1.0)
    return [Verdict(
        key[0], key[1], cond, observed=level, expected=ref,
        threshold=ref - p.get("min_drop_pct", 1.0), score=drop, diagnosis="fuel_loss",
        expr=f"max(fuel_level_pct, {int(window)} s) - fuel_level_pct >= "
             f"{p.get('min_drop_pct', 1.0)} while stopped",
        summary=(f"{dev.name} fuel down {_f(drop)} points to {_f(level)}% while the engine "
                 f"is stopped"), x=x, **kw)]


def ct2(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    fan_k, sup_k, wb_k = dev.pick("fan_speed_pct"), ("water_supply_temp", "COND"), \
        dev.pick("outdoor_wet_bulb_temp")
    kw = {"signals": (fan_k, sup_k, wb_k, ("equipment_state", "Fan_Status")),
          "alarm_metric_key": "fan_speed_pct", "fresh": fan_k in dev.fresh}
    gate = context_gate(p, dev)
    run_gate, unknown = running_gate(dev, "Fan_Status", now)
    if gate or run_gate:
        return [_gated(fan_k, gate or run_gate, **kw)]
    fan, wb = dev.last(fan_k, now), dev.last(wb_k, now, max_age_s=3600)
    sups = dev.clean(sup_k, since=now - float(p.get("window_s", 1800)))
    if unknown or _masked(dev, (fan_k, sup_k, wb_k)) or fan is None or wb is None:
        return [_held(fan_k, **kw)]
    s = slope_per_s(sups)
    if s is None:
        return [_held(fan_k, **kw)]
    rise = s * 3600.0
    approach = sups[-1].v - wb
    cond = fan >= p.get("fan_pct", 98.0) and rise > p.get("approach_rise_k_per_h", 0.5)
    return [Verdict(
        fan_k[0], fan_k[1], cond, observed=fan, expected=None,
        threshold=p.get("fan_pct", 98.0), score=rise, diagnosis="fan_saturated",
        expr=f"fan_speed_pct >= {p.get('fan_pct', 98.0)} AND slope(approach) > "
             f"{p.get('approach_rise_k_per_h', 0.5)} K/h",
        summary=(f"{dev.name} fan at {_f(fan, 0)}% with a {_f(approach)} K approach rising "
                 f"{_f(rise)} K/h"), **kw)]


def ct3(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    lvl_k, mu_k = dev.pick("basin_level_pct"), dev.pick("makeup_water_flow")
    kw = {"signals": (lvl_k, mu_k), "alarm_metric_key": "basin_level_pct",
          "fresh": lvl_k in dev.fresh}
    gate = context_gate(p, dev)
    if gate:
        return [_gated(lvl_k, gate, **kw)]
    levels = dev.clean(lvl_k, since=now - float(p.get("window_s", 1800)))
    makeup = dev.last(mu_k, now)
    s = slope_per_s(levels)
    if _masked(dev, (lvl_k, mu_k)) or makeup is None or s is None:
        return [_held(lvl_k, **kw)]
    rate = s * 3600.0
    limit = float(p.get("fall_pct_per_h", 2.0))
    return [Verdict(
        lvl_k[0], lvl_k[1], rate < -limit and makeup > 0, observed=levels[-1].v,
        threshold=-limit, score=-rate, diagnosis="basin_imbalance",
        expr=f"slope(basin_level_pct) < -{limit} %/h AND makeup_water_flow > 0",
        summary=(f"{dev.name} basin falling {_f(-rate)} %/h with make-up running at "
                 f"{_f(makeup, 2)} L/s"), **kw)]


def at1(ri: RuleInput) -> list[Verdict]:
    p, dev, now = ri.params, ri.dev, ri.now
    key = dev.pick("transfer_count")
    x = dict(ri.x(key))
    kw = {"signals": (key,), "alarm_metric_key": "transfer_count", "fresh": key in dev.fresh}
    count = dev.last(key, now, max_age_s=7200)
    if key in dev.masked or count is None:
        return [_held(key, x=x, **kw)]
    day = 86_400.0
    anchors = [a for a in x.get("h", []) if now - a[0] <= day and a[1] <= count]
    if not anchors or now - anchors[-1][0] >= 3600:
        anchors.append([round(now, 1), count])
    x["h"] = anchors
    n = count - min(a[1] for a in anchors)
    limit = int(p.get("max_transfers_24h", 2))
    return [Verdict(
        key[0], key[1], n > limit, observed=n, threshold=float(limit),
        score=n / limit if limit else None, diagnosis="transfer_rate",
        expr=f"delta(transfer_count, 24 h) > {limit}",
        summary=f"{dev.name} transferred {int(n)} times in the last day", x=x, **kw)]


#: Detector id -> rule. The sweep runs a rule only on a device where the
#: catalogue says the detector is effective (enabled, not muted, every
#: required point present).
RULES: dict[str, Callable[[RuleInput], list[Verdict]]] = {
    "CR-1": cr1, "CR-2": cr2, "CR-6": cr6,
    "CH-1": ch1, "CH-4": ch4, "CH-5": ch5, "PL-1": pl1,
    "CD-1": cd1, "CD-6": cd6,
    "VA-1": va1, "VA-2": va2,
    "PU-3": pu3,
    "RM-2": rm2, "RM-3": rm3,
    "GN-1": gn1,
    "CT-2": ct2, "CT-3": ct3,
    "AT-1": at1,
}


def run_rule(ri: RuleInput) -> list[Verdict]:
    fn = RULES.get(ri.detector_id)
    return fn(ri) if fn else []


def finite(v: float | None) -> float | None:
    """None for NaN / inf, so nothing un-JSON-able reaches a column."""
    if v is None or not math.isfinite(v):
        return None
    return float(v)
