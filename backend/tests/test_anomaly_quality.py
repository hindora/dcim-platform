"""L0 data quality (docs/33 Phase 1 unit tests).

DQ-1 fires after grace, but not while OFFLINE, and not when the whole device
went quiet (a Modbus device whose Data_Valid bit is low returns nothing at
all). DQ-2 is the bad-fraction rule, fed by BAD quality. DQ-3 catches
sentinels. DQ-4 masks single spikes and opens a finding only above
max_spikes_per_h. DQ-5 fires on a frozen supply with a moving valve, and is
silent on a frozen setpoint and on a static point with nothing coupled.
A series whose newest sample is not GOOD is masked, unconditionally.
"""

from __future__ import annotations

from app.analytics.detectors import quality
from app.analytics.detectors import rules as rl
from tests.anomaly_fakes import flat, seed_params, series, view

T = 1_800_000_000.0
ALL = {d: seed_params(d) for d in quality.QUALITY_IDS}
SCOPE = ("supply_air_temp", "return_air_temp", "air_setpoint_temp", "valve_position_pct",
         "fan_speed_pct", "inlet_temperature")


def run(dev, params=None, states=None):
    states = states if states is not None else {}

    def state(did, key):
        return states.setdefault((did, key), rl.DwellState())
    return quality.evaluate(dev, params or ALL, T, state, SCOPE)


def by(verdicts, did, key=("supply_air_temp", "")):
    return next(v for d, v in verdicts if d == did and (v.metric_key, v.instance) == key)


def crah():
    v = view("crah", poll_interval_s=30)
    v.num[("supply_air_temp", "")] = series([18 + 0.1 * (i % 3) for i in range(40)], T)
    v.num[("return_air_temp", "")] = series([30 + 0.1 * (i % 2) for i in range(40)], T)
    v.num[("air_setpoint_temp", "")] = flat(18.0, T)
    v.num[("valve_position_pct", "CHW")] = series([40 + i for i in range(40)], T)
    v.fresh = set(v.num)
    return v


# --- DQ-1 -----------------------------------------------------------------------

def test_dq1_fires_after_grace_while_the_device_still_delivers():
    dev = crah()
    dev.num[("supply_air_temp", "")] = flat(18.0, T - 400)     # 400 s > 300 s grace
    v = by(run(dev), "DQ-1")
    assert v.cond is True and v.threshold == 300
    assert ("supply_air_temp", "") in dev.masked
    dev = crah()
    dev.num[("supply_air_temp", "")] = flat(18.0, T - 200)     # inside grace
    assert by(run(dev), "DQ-1").cond is False


def test_dq1_holds_while_offline():
    dev = crah()
    dev.online = False
    dev.num[("supply_air_temp", "")] = flat(18.0, T - 4000)
    assert by(run(dev), "DQ-1").cond is None


def test_dq1_is_silent_when_the_whole_device_is_quiet():
    # Modbus Data_Valid low: the poll returns no samples for ANY point. That
    # is "not ready", which is not one sensor dropping out.
    dev = crah()
    for k, s in list(dev.num.items()):
        dev.num[k] = flat(s[-1].v, T - 900)
    assert by(run(dev), "DQ-1").cond is False


def test_dq1_grace_follows_the_series_own_cadence():
    dev = crah()
    # A slow point polled every 600 s: 3 x 600 = 1800 s of grace.
    dev.num[("supply_air_temp", "")] = series([18.0] * 5, T - 1000, step=600)
    v = by(run(dev), "DQ-1")
    assert v.threshold == 1800 and v.cond is False


# --- DQ-2 -----------------------------------------------------------------------

def test_dq2_bad_fraction_fed_by_bad_quality():
    dev = crah()
    good = series([18.0] * 20, T)
    dev.num[("supply_air_temp", "")] = [
        rl.Sample(s.ts, s.v, "bad" if i >= 8 else "good") for i, s in enumerate(good)]
    v = by(run(dev), "DQ-2")
    assert v.cond is True and v.observed == 0.6
    assert ("supply_air_temp", "") in dev.masked
    dev = crah()
    dev.num[("supply_air_temp", "")] = [
        rl.Sample(s.ts, s.v, "bad" if i == 19 else "good") for i, s in enumerate(good)]
    v = by(run(dev), "DQ-2")
    assert v.cond is False
    # ...but one BAD newest sample still masks the series: it is no evidence.
    assert ("supply_air_temp", "") in dev.masked


def test_dq2_needs_five_samples():
    dev = crah()
    dev.num[("supply_air_temp", "")] = series([18.0] * 3, T, q="bad")
    assert by(run(dev), "DQ-2").cond is None


# --- DQ-3 -----------------------------------------------------------------------

def test_dq3_sentinels_and_bounds():
    for bad in (-40.0, 85.0, -127.0, 327.67, 45.0):
        dev = crah()
        dev.num[("supply_air_temp", "")] = series([18.0] * 30 + [bad], T)
        v = by(run(dev), "DQ-3")
        assert v.cond is True, bad
        assert T in dev.dropped[("supply_air_temp", "")]
        # Dropped from what the rules may read.
        assert dev.clean(("supply_air_temp", ""))[-1].v == 18.0
    dev = crah()
    assert by(run(dev), "DQ-3").cond is False


def test_dq3_opens_after_three_samples():
    p = ALL["DQ-3"]
    st = rl.DwellState()
    events = []
    for i in range(4):
        dev = crah()
        dev.num[("supply_air_temp", "")] = series([18.0] * 30 + [-40.0] * (i + 1), T + i * 60)
        v = by(quality.evaluate(dev, {"DQ-3": p}, T + i * 60, lambda d, k: st, SCOPE),
               "DQ-3")
        events.append(rl.step(st, v, p, T + i * 60))
    assert events == [None, None, rl.OPEN, rl.TOUCH]


# --- DQ-4 -----------------------------------------------------------------------

def test_dq4_masks_a_single_spike_without_a_finding():
    dev = crah()
    vals = [18.0 + 0.05 * (i % 3) for i in range(40)]
    vals[30] = 26.0
    dev.num[("supply_air_temp", "")] = series(vals, T)
    v = by(run(dev), "DQ-4")
    assert v.cond is False
    spike_ts = dev.num[("supply_air_temp", "")][30].ts
    assert spike_ts in dev.dropped[("supply_air_temp", "")]


def test_dq4_opens_above_max_spikes_per_hour():
    dev = crah()
    vals = [18.0 + 0.05 * (i % 3) for i in range(60)]
    for i in range(3, 58, 7):
        vals[i] = 26.0
    dev.num[("supply_air_temp", "")] = series(vals, T)
    v = by(run(dev), "DQ-4")
    assert v.cond is True and v.observed > 6


def test_dq4_does_not_call_a_step_a_spike():
    dev = crah()
    dev.num[("supply_air_temp", "")] = series([18.0] * 20 + [22.0] * 20, T)
    assert by(run(dev), "DQ-4").cond is False


# --- DQ-5 -----------------------------------------------------------------------

def test_dq5_frozen_supply_with_a_moving_valve():
    dev = crah()
    dev.num[("supply_air_temp", "")] = flat(18.4, T)
    v = by(run(dev), "DQ-5")
    assert v.cond is True and ("supply_air_temp", "") in dev.masked


def test_dq5_silent_on_a_frozen_setpoint_and_a_still_valve():
    dev = crah()
    dev.num[("supply_air_temp", "")] = flat(18.4, T)
    dev.num[("valve_position_pct", "CHW")] = flat(40.0, T)
    assert by(run(dev), "DQ-5").cond is False
    # The setpoint has nothing coupled: never judged.
    assert not [v for d, v in run(crah()) if d == "DQ-5" and v.metric_key == "air_setpoint_temp"]


def test_dq5_silent_on_a_static_snmp_point():
    # A file-backed SNMP probe reads the same inlet forever; nothing coupled
    # moves on a rack sensor (no cpu_utilization), so it is not flagged.
    dev = view("sensor")
    dev.num[("inlet_temperature", "")] = flat(22.0, T, step=120)
    assert by(run(dev), "DQ-5", ("inlet_temperature", "")).cond is False
    assert ("inlet_temperature", "") not in dev.masked


def test_a_disabled_check_does_not_run():
    dev = crah()
    dev.num[("supply_air_temp", "")] = flat(18.4, T)
    out = run(dev, params={"DQ-1": ALL["DQ-1"]})
    assert {d for d, _ in out} == {"DQ-1"}
    assert ("supply_air_temp", "") not in dev.masked
