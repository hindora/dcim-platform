"""L2 rules and the dwell framework (docs/33 Phase 1 unit tests).

Per rule: it fires after EXACTLY the set dwell, its gate blocks it, it
clears after the clear dwell, and it HOLDS (neither opens nor clears) while
a required input is masked by L0. Params are the 0112 seed values, read
from the migration itself.
"""

from __future__ import annotations

import pytest

from app.analytics.detectors import rules as rl
from tests.anomaly_fakes import flat, running, seed_params, series, view

T0 = 1_800_000_000.0
STEP = 60.0


def drive(det_id, make_view, sweeps, *, params=None, ctx=None, st=None, t0=T0):
    """Run a rule for `sweeps` sweeps; returns (events, state, last verdict)."""
    p = params or seed_params(det_id)
    st = st or rl.DwellState()
    events, v = [], None
    for i in range(sweeps):
        t = t0 + i * STEP
        dv = make_view(t, i)
        ri = rl.RuleInput(det_id, p, dv, ctx or rl.Context(), t,
                          active=lambda k: st.active, x=lambda k: st.x)
        vs = rl.run_rule(ri)
        v = vs[0]
        events.append(rl.step(st, v, p, t))
    return events, st, v


def first(events, what):
    return events.index(what) if what in events else None


# --- CR-1 ---------------------------------------------------------------------

def crah(t, *, supply=24.0, setpoint=20.0, valve=100.0, run=True, since=None, **kw):
    v = view("crah", **kw)
    v.num[("supply_air_temp", "")] = flat(supply, t)
    v.num[("air_setpoint_temp", "")] = flat(setpoint, t)
    v.num[("valve_position_pct", "CHW")] = flat(valve, t)
    v.num[("return_air_temp", "")] = flat(supply + 10, t)
    v.num[("fan_speed_pct", "")] = flat(60.0, t)
    v.bools[("equipment_state", "Unit_Running")] = running(t, since or T0 - 7200, run)
    v.fresh = set(v.num)
    return v


def test_cr1_fires_after_exactly_the_set_dwell():
    events, st, v = drive("CR-1", lambda t, i: crah(t), 20)
    # 900 s dwell on a 60 s sweep: pending at sweep 0, open at sweep 15.
    assert first(events, rl.OPEN) == 15
    assert all(e is None for e in events[:15])
    assert events[16:] == [rl.TOUCH] * 4
    assert st.first_seen == T0
    assert v.alarm_metric_key == "supply_air_temp"


@pytest.mark.parametrize("valve,diag", [(100.0, "chw_starved"), (96.0, "chw_starved"),
                                        (40.0, "control_fault")])
def test_cr1_splits_the_diagnosis_on_the_valve(valve, diag):
    _, _, v = drive("CR-1", lambda t, i: crah(t, valve=valve), 1)
    assert v.diagnosis == diag
    assert v.cond is True


@pytest.mark.parametrize("make,gate", [
    (lambda t, i: crah(t, run=False), "not_running"),
    (lambda t, i: crah(t, since=t - 300), "running_min"),
    (lambda t, i: crah(t, fire_predischarge=True), "fire_predischarge"),
    (lambda t, i: crah(t, maintenance=True), "maintenance"),
])
def test_cr1_gates_block_it(make, gate):
    events, st, v = drive("CR-1", make, 30)
    assert rl.OPEN not in events and not st.active
    assert v.gate == gate


def test_cr1_clears_after_the_clear_dwell_with_hysteresis():
    st = rl.DwellState()
    drive("CR-1", lambda t, i: crah(t), 16, st=st)
    assert st.active
    t1 = T0 + 16 * STEP
    # 2.5 K over: below the 3.0 K raise margin but above the 2.0 K clear
    # margin, so an active finding stays active.
    events, st, _ = drive("CR-1", lambda t, i: crah(t, supply=22.5), 10, st=st, t0=t1)
    assert set(events) == {rl.TOUCH}
    t2 = t1 + 10 * STEP
    events, st, _ = drive("CR-1", lambda t, i: crah(t, supply=21.5), 10, st=st, t0=t2)
    # 300 s clear dwell: clearing starts at sweep 0, resolves at sweep 5.
    assert first(events, rl.RESOLVE) == 5 and not st.active


def test_cr1_holds_under_a_mask():
    st = rl.DwellState()
    drive("CR-1", lambda t, i: crah(t), 16, st=st)
    assert st.active

    def masked(t, i):
        v = crah(t, supply=19.0)          # would clear, if it were believed
        v.masked.add(("supply_air_temp", ""))
        return v

    events, st, v = drive("CR-1", masked, 30, st=st, t0=T0 + 16 * STEP)
    assert set(events) == {None} and st.active and v.cond is None
    # And a mask does not open a finding either.
    events, st2, _ = drive("CR-1", lambda t, i: (lambda x: (x.masked.add(
        ("supply_air_temp", "")), x)[1])(crah(t)), 30)
    assert rl.OPEN not in events and not st2.active


def test_a_non_good_newest_sample_is_no_evidence():
    def bad(t, i):
        v = crah(t)
        v.num[("supply_air_temp", "")] = [*series([24.0] * 40, t)[:-1],
                                          rl.Sample(t, 24.0, "bad")]
        return v
    # The rule itself reads only GOOD samples; the quality pass is what masks
    # the series (tests/test_anomaly_quality.py). Here: BAD is never "last".
    _, _, v = drive("CR-1", bad, 1)
    assert v.observed == 24.0


# --- CR-2 -----------------------------------------------------------------------

def test_cr2_low_delta_t_and_low_fan_gate():
    def low(t, i, fan=60.0):
        v = crah(t, supply=18.0)
        v.num[("return_air_temp", "")] = flat(21.0, t)
        v.num[("fan_speed_pct", "")] = flat(fan, t)
        return v
    events, _, v = drive("CR-2", low, 20)
    assert first(events, rl.OPEN) == 15 and v.alarm_metric_key == "return_air_temp"
    events, _, v = drive("CR-2", lambda t, i: low(t, i, fan=20.0), 20)
    assert rl.OPEN not in events and v.gate == "low_fan"


# --- CR-6 / VA-2 hunting ----------------------------------------------------------

def zigzag(t, n=60, lo=40.0, hi=60.0):
    return series([hi if i % 4 < 2 else lo for i in range(n)], t)


def test_cr6_hunting_opens_immediately_and_clears_after_1800_s():
    def hunt(t, i):
        v = crah(t)
        v.num[("valve_position_pct", "CHW")] = zigzag(t)
        return v
    events, st, v = drive("CR-6", hunt, 2)
    assert events[0] == rl.OPEN and v.observed >= 6
    events, st, _ = drive("CR-6", lambda t, i: crah(t, valve=55.0), 40, st=st,
                          t0=T0 + 2 * STEP)
    assert first(events, rl.RESOLVE) == 30


def test_va2_ignores_moves_inside_the_deadband():
    def jitter(t, i):
        v = view("valve")
        v.num[("valve_position_pct", "MEASURED")] = series(
            [50.0 + (2.0 if k % 2 else 0.0) for k in range(60)], t)
        return v
    events, _, v = drive("VA-2", jitter, 5)
    assert rl.OPEN not in events and v.observed == 0


# --- CH-1 -----------------------------------------------------------------------

def chiller(t, *, supply=8.5, sp=7.0, load=70.0, since=None, known=True):
    v = view("chiller")
    v.num[("water_supply_temp", "CHW")] = flat(supply, t)
    v.num[("water_setpoint_temp", "CHW")] = flat(sp, t)
    v.num[("compressor_load_pct", "")] = flat(load, t)
    v.bools[("equipment_state", "Chiller_Running")] = running(t, since or T0 - 7200,
                                                              known=known)
    v.fresh = set(v.num)
    return v


def test_ch1_fires_after_dwell_and_its_gates():
    events, _, _ = drive("CH-1", lambda t, i: chiller(t), 20)
    assert first(events, rl.OPEN) == 15
    # Restart exclusion: a real transition 300 s ago.
    _, _, v = drive("CH-1", lambda t, i: chiller(t, since=t - 300), 1)
    assert v.gate == "restart_exclusion"
    # Running 600 s with no observed transition: running_min, not restart.
    _, _, v = drive("CH-1", lambda t, i: chiller(t, since=t - 600, known=False), 1)
    assert v.gate == "running_min"
    # At full load a chiller short of setpoint is capacity, not a fault.
    events, _, v = drive("CH-1", lambda t, i: chiller(t, load=99.0), 20)
    assert rl.OPEN not in events and v.gate == "at_capacity"


# --- CH-5 -----------------------------------------------------------------------

def test_ch5_opens_after_two_fresh_samples_projected_to_trip():
    def rising(t, i):
        v = chiller(t)
        v.num[("water_return_temp", "COND")] = series(
            [36.0 + 0.25 * k for k in range(20)], t)   # 0.5 K/min at 30 s steps
        v.num[("water_pressure", "COND")] = flat(900.0, t)
        v.fresh = {("water_return_temp", "COND")}
        return v
    events, _, v = drive("CH-5", rising, 3)
    assert events[:2] == [None, rl.OPEN]
    assert v.score == pytest.approx(0.5)


# --- CD-1 / CD-6 ------------------------------------------------------------------

def cdu(t, *, supply=30.0, sp=27.0, valve=100.0):
    v = view("cdu")
    v.num[("water_supply_temp", "")] = flat(supply, t)
    v.num[("water_setpoint_temp", "")] = flat(sp, t)
    v.num[("valve_position_pct", "FACILITY_CHW")] = flat(valve, t)
    v.bools[("equipment_state", "Unit_Running")] = running(t, T0 - 7200)
    v.fresh = set(v.num)
    return v


def test_cd1_needs_the_valve_wide_open_and_supply_over_by_2k():
    events, _, _ = drive("CD-1", lambda t, i: cdu(t), 8)
    assert first(events, rl.OPEN) == 5            # 300 s dwell
    events, _, _ = drive("CD-1", lambda t, i: cdu(t, valve=80.0), 8)
    assert rl.OPEN not in events
    events, _, _ = drive("CD-1", lambda t, i: cdu(t, supply=28.9), 8)
    assert rl.OPEN not in events


def test_cd6_against_the_room_dew_point():
    ctx = rl.Context(room_dew_max={"room-1": 16.5})
    events, _, v = drive("CD-6", lambda t, i: cdu(t, supply=18.0), 12, ctx=ctx)
    assert first(events, rl.OPEN) == 10 and v.expected == 16.5
    events, _, v = drive("CD-6", lambda t, i: cdu(t, supply=18.0), 3)
    assert v.cond is None                          # no dew point: hold


# --- VA-1 -------------------------------------------------------------------------

def valve(t, *, meas=30.0, cmd=60.0, cmd_series=None):
    v = view("valve")
    v.num[("valve_position_pct", "MEASURED")] = flat(meas, t)
    v.num[("valve_position_pct", "COMMANDED")] = cmd_series or flat(cmd, t)
    v.fresh = set(v.num)
    return v


def test_va1_mismatch_settled_command_only():
    events, _, v = drive("VA-1", lambda t, i: valve(t), 12)
    assert first(events, rl.OPEN) == 10 and v.diagnosis == "actuator"
    def moving(t, i):
        return valve(t, cmd_series=series([20.0] * 38 + [40.0, 60.0], t))
    _, _, v = drive("VA-1", moving, 1)
    assert v.gate == "settling"


# --- PU-3 -------------------------------------------------------------------------

def pump(t, *, status=True, speed=80.0, flow=20.0):
    v = view("pump")
    v.num[("water_flow", "")] = flat(flow, t)
    v.num[("pump_speed_pct", "")] = flat(speed, t)
    v.bools[("equipment_state", "Run_Status")] = running(t, T0 - 7200, status)
    v.fresh = set(v.num)
    return v


def test_pu3_proof_disagrees_with_process():
    events, st, v = drive("PU-3", lambda t, i: pump(t, status=False), 3)
    assert events[:2] == [None, rl.OPEN] and v.diagnosis == "running_unproven"
    assert v.alarm_metric_key == "water_flow"
    # Learned mean flow, then proven-running with no flow.
    st = rl.DwellState()
    drive("PU-3", lambda t, i: pump(t), 5, st=st)
    assert st.x["mf"] == pytest.approx(20.0)
    events, st, v = drive("PU-3", lambda t, i: pump(t, flow=0.2), 3, st=st,
                          t0=T0 + 5 * STEP)
    assert rl.OPEN in events and v.diagnosis == "proven_no_flow"
    events, st, _ = drive("PU-3", lambda t, i: pump(t), 4, st=st, t0=T0 + 8 * STEP)
    assert first(events, rl.RESOLVE) == 2          # 3 clear samples


# --- RM-2 / RM-3 --------------------------------------------------------------------

def probe(t, inlet, *, n=10, step=120.0, name="SEN-1"):
    v = view("sensor", name=name)
    v.num[("inlet_temperature", "")] = series(inlet if isinstance(inlet, list)
                                              else [inlet] * n, t, step)
    v.fresh = set(v.num)
    return v


def test_rm2_rate_of_change_after_three_samples():
    def ramp(t, i):
        return probe(t, [20 + 0.9 * k for k in range(10)])   # 27 K/h
    events, _, v = drive("RM-2", ramp, 4)
    assert first(events, rl.OPEN) == 2 and v.score == pytest.approx(27.0)
    events, _, _ = drive("RM-2", lambda t, i: probe(t, [20 + 0.3 * k for k in range(10)]), 4)
    assert rl.OPEN not in events


def test_rm3_hot_spot_against_the_room_p90():
    ctx = rl.Context(room_inlet={"room-1": [22.0, 22.5, 23.0, 22.8, 23.1, 29.0]})
    events, _, v = drive("RM-3", lambda t, i: probe(t, 29.0), 17, ctx=ctx)
    # p90 of six by nearest rank is the 6th value: the hot rack itself.
    assert v.expected == 29.0 and rl.OPEN not in events
    ctx = rl.Context(room_inlet={"room-1": [22.0, 22.5, 23.0, 22.8, 23.1, 23.0, 22.9, 22.7,
                                            23.2, 22.6, 29.0]})
    events, _, v = drive("RM-3", lambda t, i: probe(t, 29.0), 17, ctx=ctx)
    assert first(events, rl.OPEN) == 15 and v.expected == 23.2
    ctx = rl.Context(room_inlet={"room-1": [22.0, 29.0]})
    _, _, v = drive("RM-3", lambda t, i: probe(t, 29.0), 1, ctx=ctx)
    assert v.cond is None                          # fewer than 5 room sensors


# --- GN-1 / CT-3 / AT-1 / CT-2 / PL-1 --------------------------------------------------

def test_gn1_fuel_falling_while_stopped():
    def gen(t, i, run=False):
        v = view("generator")
        v.num[("fuel_level_pct", "")] = flat(80.0 - 0.05 * i, t)
        v.bools[("equipment_state", "Engine_Running")] = running(t, T0 - 7200, run)
        v.fresh = set(v.num)
        return v
    events, _, v = drive("GN-1", gen, 70)
    assert rl.OPEN in events and v.diagnosis == "fuel_loss"
    events, _, v = drive("GN-1", lambda t, i: gen(t, i, run=True), 70)
    assert rl.OPEN not in events and v.gate == "running"


def test_ct3_basin_falling_with_makeup_running():
    p = {**seed_params("CT-3"), "dwell_s": 0}
    def tower(t, i, mu=1.0):
        v = view("cooling_tower")
        v.num[("basin_level_pct", "")] = series([80 - 0.05 * k for k in range(40)], t)
        v.num[("makeup_water_flow", "")] = flat(mu, t)
        v.fresh = set(v.num)
        return v
    events, _, _ = drive("CT-3", tower, 1, params=p)
    assert events == [rl.OPEN]
    events, _, _ = drive("CT-3", lambda t, i: tower(t, i, mu=0.0), 1, params=p)
    assert events == [None]


def test_at1_transfers_in_a_day():
    def ats(t, i):
        v = view("ats")
        v.num[("transfer_count", "")] = flat(10.0 + (i // 3), t)
        v.fresh = set(v.num)
        return v
    events, _, v = drive("AT-1", ats, 12)
    assert rl.OPEN in events and v.observed == 3


def test_ct2_and_pl1_evaluate_their_conditions():
    def tower(t, i):
        v = view("cooling_tower")
        v.num[("fan_speed_pct", "")] = flat(100.0, t)
        v.num[("water_supply_temp", "COND")] = series([28 + 0.01 * k for k in range(60)], t)
        v.num[("outdoor_wet_bulb_temp", "")] = flat(22.0, t)
        v.bools[("equipment_state", "Fan_Status")] = running(t, T0 - 7200)
        return v
    _, _, v = drive("CT-2", tower, 1)
    assert v.cond is True and v.score == pytest.approx(1.2)

    def plant(t, i):
        v = view("chiller")
        v.num[("water_return_temp", "CHW")] = flat(9.5, t)
        v.num[("water_supply_temp", "CHW")] = flat(7.0, t)
        v.num[("water_flow", "CHW")] = series([100 + k for k in range(40)], t)
        return v
    _, _, v = drive("PL-1", plant, 1)
    assert v.cond is True and v.threshold == pytest.approx(3.3)


# --- the framework ---------------------------------------------------------------------

def test_chattering_is_held_open_after_three_reopens():
    p = {"dwell_s": 0, "clear_dwell_s": 60, "reopen_window_s": 1800}
    st = rl.DwellState(active=True, reopens=4)
    off = rl.Verdict("x", "", False)
    assert rl.step(st, off, p, T0) is None
    assert rl.step(st, off, p, T0 + 120) is None          # 60 s would have cleared it
    assert rl.step(st, off, p, T0 + 1800) == rl.RESOLVE


def test_the_wire_form_round_trips():
    st = rl.DwellState(active=True, pending_since=None, clear_since=T0, n_false=2,
                       first_seen=T0 - 900, reopens=1, x={"mf": 3.2})
    assert rl.DwellState.from_wire(st.to_wire()) == st
    assert rl.DwellState().to_wire() == {"a": 0}


def test_reversals_counts_direction_changes_of_the_step():
    assert rl.reversals([0, 10, 0, 10, 0, 10], 5) == 4
    assert rl.reversals([0, 3, 0, 3, 0], 5) == 0
    assert rl.reversals([0, 2, 4, 6, 8, 10], 5) == 0
