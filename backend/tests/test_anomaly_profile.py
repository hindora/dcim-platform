"""Phase 3a (docs/35): learning a device's usual per hour, and judging it against it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.analytics.baselines import HourRow, fit_profile
from app.analytics.detectors import residual
from app.analytics.detectors.rules import (
    DeviceView,
    DwellState,
    RuleInput,
    Sample,
    step,
)
from tests.anomaly_fakes import seed_params

P = seed_params("SV-3")
DAY0 = datetime(2026, 10, 1, tzinfo=UTC)


def _workdays(days: int, hours=range(7, 14), value=30.0, jitter=1.0, samples=60):
    """A part-time estate: data only 07:00-13:59 UTC on each day."""
    out = []
    for d in range(days):
        for h in hours:
            v = value + jitter * (((d * 7 + h) % 5) - 2) / 2   # deterministic wobble
            out.append(HourRow(DAY0 + timedelta(days=d, hours=h), v, samples))
    return out


# --- the fit -------------------------------------------------------------------

def test_enough_part_time_history_learns_the_hours_it_has_seen():
    fit = fit_profile(_workdays(25), P)
    assert fit.status == "ok"
    # 07:00 is each day's cold start, so it is never learned
    assert set(fit.model["hod"]) == {str(h) for h in range(8, 14)}
    med, sigma, n = fit.model["hod"]["10"]
    assert abs(med - 30.0) <= 1.0 and sigma >= P["sigma_floor"] and n == 25


def test_too_little_history_is_learning_not_judging():
    fit = fit_profile(_workdays(5), P)
    assert fit.status == "thin" and fit.model["hours"] < P["min_hours"]


def test_the_first_hour_after_a_gap_is_left_out():
    fit = fit_profile(_workdays(25), P)
    assert "7" not in fit.model["hod"]           # every 07:00 follows the overnight gap
    assert fit.excluded == 25


def test_thin_hours_are_refused():
    rows = _workdays(25)
    rows[10] = HourRow(rows[10].bucket, 99.0, 5)  # 5 samples where the series has 60
    fit = fit_profile(rows, P)
    assert fit.excluded == 26


def test_maintenance_is_not_learned_as_normal():
    rows = [HourRow(r.bucket, 95.0 if r.bucket.day == 3 else r.value, r.samples)
            for r in _workdays(25)]
    span = (DAY0 + timedelta(days=2), DAY0 + timedelta(days=3))
    fit = fit_profile(rows, P, excluded_spans=[span])
    assert abs(fit.model["hod"]["10"][0] - 30.0) <= 1.0


def test_one_night_cannot_rewrite_normal():
    old = fit_profile(_workdays(25), P).model
    shifted = fit_profile(_workdays(25, value=60.0), P, previous=old)
    med_old, sig_old, _ = old["hod"]["10"]
    step_allowed = max(abs(med_old) * 0.10, sig_old)
    assert shifted.model["hod"]["10"][0] <= med_old + step_allowed + 1e-6


# --- scoring -------------------------------------------------------------------

def _baseline(med=30.0, sigma=2.0, hours=range(0, 24)):
    return {"status": "ok", "model": {"hod": {str(h): [med, sigma, 20] for h in hours}}}


def _view(values, now, step_s=60.0, fresh=True):
    dev = DeviceView(device_id="d1", name="SRV01", device_type="server", online=True)
    key = ("cpu_utilization", "")
    dev.num[key] = [Sample(now - (len(values) - 1 - i) * step_s, v) for i, v in enumerate(values)]
    if fresh:
        dev.fresh.add(key)
    return dev, key


def _ri(dev, now, state, base=None, params=None):
    return RuleInput("SV-3", {**P, **(params or {})}, dev, None, now,
                     active=lambda k: state.active, x=lambda k: state.x,
                     baseline=lambda k: base)


def _run(values_by_sweep, base, params=None, start=None):
    """Sweep once a minute; returns the dwell events and the final state."""
    now = start or datetime(2026, 11, 3, 10, 0, tzinfo=UTC).timestamp()
    st, events, history = DwellState(), [], []
    for vals in values_by_sweep:
        history.extend(vals)
        dev, _ = _view(history[-60:], now)
        (v,) = residual.run_rule(_ri(dev, now, st, base, params))
        events.append(step(st, v, {**P, **(params or {})}, now))
        now += 60
    return events, st, v


def test_usual_behaviour_stays_quiet():
    events, st, v = _run([[30.0 + (i % 3 - 1) * 0.5] for i in range(120)], _baseline())
    assert "open" not in events and not st.active and v.cond is False
    assert abs(st.x["e"]) < 1.0 and st.x["sp"] < 5.0 and st.x["sn"] < 5.0


def test_a_large_step_opens_a_finding_after_its_dwell():
    usual = [[30.0]] * 60
    spike = [[92.0]] * 40
    events, st, v = _run(usual + spike, _baseline(), params={"dwell_s": 900})
    assert "open" in events and st.active
    assert "92" in v.summary and "usual 30" in v.summary


def test_a_slow_drift_is_caught_by_the_cusum():
    # +0.05 a minute: 3 a hour, far below the 4-sigma step but steady.
    drift = [[30.0 + 0.05 * i] for i in range(240)]
    events, _st, _v = _run(drift, _baseline(sigma=2.0), params={"dwell_s": 0})
    assert "open" in events


def test_without_a_baseline_it_holds_rather_than_clears():
    dev, _key = _view([90.0] * 40, 1_800_000_000.0)
    (v,) = residual.run_rule(_ri(dev, 1_800_000_000.0, DwellState(), None))
    assert v.cond is None and v.diagnosis == "no_baseline"


def test_a_learning_baseline_holds():
    dev, _key = _view([90.0] * 40, 1_800_000_000.0)
    learning = {"status": "thin", "model": {}}
    (v,) = residual.run_rule(_ri(dev, 1_800_000_000.0, DwellState(), learning))
    assert v.cond is None and v.diagnosis == "learning"


def test_an_hour_never_seen_holds():
    now = datetime(2026, 11, 3, 22, 0, tzinfo=UTC).timestamp()
    dev, _key = _view([90.0] * 40, now)
    (v,) = residual.run_rule(_ri(dev, now, DwellState(), _baseline(hours=range(7, 14))))
    assert v.cond is None and v.diagnosis == "hour_not_learned"


def test_the_morning_cold_start_holds():
    now = datetime(2026, 11, 3, 7, 10, tzinfo=UTC).timestamp()
    dev = DeviceView(device_id="d1", name="SRV01", device_type="server", online=True)
    key = ("cpu_utilization", "")
    # yesterday afternoon, then the last ten minutes
    dev.num[key] = [Sample(now - 16 * 3600, 30.0)] + [
        Sample(now - 600 + 60 * i, 90.0) for i in range(10)]
    dev.fresh.add(key)
    (v,) = residual.run_rule(_ri(dev, now, DwellState(), _baseline()))
    assert v.cond is None and v.diagnosis == "cold_start"


def test_phase_imbalance_only_counts_upwards():
    dev = DeviceView(device_id="d2", name="MPP1", device_type="mpp", online=True)
    key = ("phase_imbalance_pct", "")
    now = datetime(2026, 11, 3, 10, 0, tzinfo=UTC).timestamp()
    dev.num[key] = [Sample(now - 60 * i, 0.1) for i in range(40)][::-1]
    dev.fresh.add(key)
    st = DwellState()
    ri = RuleInput("PD-2", seed_params("PD-2"), dev, None, now,
                   active=lambda k: False, x=lambda k: st.x,
                   baseline=lambda k: _baseline(med=8.0, sigma=0.5))
    (v,) = residual.run_rule(ri)
    assert v.cond is False                      # far BELOW usual is not a fault here


# --- SV-4: power given CPU ---------------------------------------------------

from app.analytics.baselines import PairRow, fit_ols  # noqa: E402
from app.analytics.detectors.rules import Context  # noqa: E402

P4 = seed_params("SV-4")


def _pairs(days=25, a=150.0, b=3.0, noise=2.0):
    out = []
    for d in range(days):
        for h in range(7, 14):
            cpu = 20 + ((d * 7 + h) * 13) % 60          # 20..79 %, varied
            jitter = noise * ((((d * 7 + h) % 5) - 2) / 2)
            out.append(PairRow(DAY0 + timedelta(days=d, hours=h), a + b * cpu + jitter, cpu, 60))
    return out


def test_the_regression_recovers_the_link():
    fit = fit_ols(_pairs(), P4)
    assert fit.status == "ok"
    assert abs(fit.model["b"][0] - 3.0) < 0.1 and abs(fit.model["a"] - 150.0) < 5.0
    assert fit.model["r2"] > 0.9 and fit.model["x_lo"][0] < 30 < 70 < fit.model["x_hi"][0]


def test_a_weak_link_is_not_a_baseline():
    rows = [PairRow(r.bucket, 300.0 + ((i * 37) % 50), r.x, 60) for i, r in enumerate(_pairs())]
    fit = fit_ols(rows, P4)
    assert fit.status == "thin" and fit.model["reason"] == "weak_link"


def test_a_driver_that_never_moves_teaches_nothing():
    rows = [PairRow(r.bucket, r.y, 40.0, 60) for r in _pairs()]
    fit = fit_ols(rows, P4)
    assert fit.status == "thin" and fit.model["reason"] == "driver_does_not_vary"


def _ols_base(a=150.0, b=3.0, sigma=4.0, lo=20.0, hi=80.0):
    return {"status": "ok", "model": {"a": a, "b": [b], "sigma": sigma, "x_lo": [lo],
                                      "x_hi": [hi]}}


def _server(power, cpu, now):
    dev = DeviceView(device_id="d1", name="SRV07", device_type="server", online=True)
    for key, v in (("power_draw", power), ("cpu_utilization", cpu)):
        dev.num[(key, "")] = [Sample(now - 60 * i, v) for i in range(40)][::-1]
        dev.fresh.add((key, ""))
    return dev


def _sv4(power, cpu, base, st=None, now=None):
    now = now or datetime(2026, 11, 3, 10, 0, tzinfo=UTC).timestamp()
    st = st or DwellState()
    ri = RuleInput("SV-4", P4, _server(power, cpu, now), None, now,
                   active=lambda k: st.active, x=lambda k: st.x, baseline=lambda k: base)
    (v,) = residual.run_rule(ri)
    return v


def test_power_that_follows_its_cpu_is_quiet():
    v = _sv4(150 + 3 * 50, 50, _ols_base())
    assert v.cond is False and abs(v.expected - 300.0) < 1e-6


def test_power_far_above_what_its_cpu_explains_counts():
    v = _sv4(420, 50, _ols_base())                  # 300 W expected
    assert v.cond is True and "more than the 300 W" in v.summary


def test_a_load_outside_what_was_learned_is_not_judged():
    v = _sv4(500, 99, _ols_base(hi=80.0))
    assert v.cond is None and v.diagnosis == "outside_learned_range"


# --- TH-1: intake above the room's supply ------------------------------------

P1 = seed_params("TH-1")


def _th1(inlet, supply, usual_gap=3.0, sigma=0.3):
    now = datetime(2026, 11, 3, 10, 0, tzinfo=UTC).timestamp()
    dev = DeviceView(device_id="d1", name="SRV09", device_type="server", room_id="r1",
                     online=True)
    dev.num[("inlet_temperature", "")] = [Sample(now - 60 * i, inlet) for i in range(40)][::-1]
    dev.fresh.add(("inlet_temperature", ""))
    st = DwellState()
    base = {"status": "ok", "model": {"of": "inlet_minus_room_supply",
                                      "hod": {"10": [usual_gap, sigma, 20]}}}
    ri = RuleInput("TH-1", P1, dev, Context(room_supply={"r1": supply}), now,
                   active=lambda k: st.active, x=lambda k: st.x, baseline=lambda k: base)
    (v,) = residual.run_rule(ri)
    return v


def test_a_raised_setpoint_moves_both_and_is_not_an_anomaly():
    assert _th1(inlet=25.0, supply=22.0).cond is False      # usual 3 K gap
    assert _th1(inlet=27.0, supply=24.0).cond is False      # setpoint +2 K: still 3 K


def test_hot_air_reaching_one_rack_widens_its_gap():
    v = _th1(inlet=29.0, supply=22.0)
    assert v.cond is True and "7.0 K above the room's supply air" in v.summary


def test_no_room_supply_is_held():
    now = datetime(2026, 11, 3, 10, 0, tzinfo=UTC).timestamp()
    dev = DeviceView(device_id="d1", name="SRV09", device_type="server", room_id="r9",
                     online=True)
    dev.num[("inlet_temperature", "")] = [Sample(now - 60 * i, 25.0) for i in range(40)][::-1]
    base = {"status": "ok", "model": {"hod": {"10": [3.0, 0.3, 20]}}}
    ri = RuleInput("TH-1", P1, dev, Context(), now, active=lambda k: False,
                   x=lambda k: {}, baseline=lambda k: base)
    (v,) = residual.run_rule(ri)
    assert v.cond is None and v.diagnosis == "no_room_reference"


def test_a_fast_polled_series_with_a_full_buffer_is_not_a_cold_start():
    from app.analytics.window import KEEP_MAX
    now = datetime(2026, 11, 3, 10, 0, tzinfo=UTC).timestamp()
    samples = [Sample(now - 20 * i, 30.0) for i in range(KEEP_MAX)][::-1]   # 21 min at 20 s
    assert residual._resumed_at(samples, now) is None


def test_a_series_that_only_just_began_is_a_cold_start():
    now = datetime(2026, 11, 3, 10, 0, tzinfo=UTC).timestamp()
    samples = [Sample(now - 60 * i, 30.0) for i in range(10)][::-1]
    assert residual._resumed_at(samples, now) == samples[0].ts


# --- step 3: several drivers, a transform, the battery gap --------------------

from app.analytics.l3_specs import SPECS, transform  # noqa: E402


def test_two_drivers_are_fitted_together():
    rows = []
    for d in range(25):
        for h in range(7, 14):
            cpu = 20 + ((d * 7 + h) * 13) % 60
            inlet = 20 + ((d * 5 + h) * 7) % 8
            y = 30 + 0.4 * cpu + 1.5 * inlet + 0.3 * (((d + h) % 5) - 2)
            rows.append(PairRow(DAY0 + timedelta(days=d, hours=h), y, (cpu, inlet), 60))
    fit = fit_ols(rows, seed_params("SV-1"), drivers=("cpu_utilization", "inlet_temperature"))
    assert fit.status == "ok" and fit.model["drivers"] == ["cpu_utilization", "inlet_temperature"]
    b_cpu, b_inlet = fit.model["b"]
    assert abs(b_cpu - 0.4) < 0.05 and abs(b_inlet - 1.5) < 0.1


def test_runtime_is_a_straight_line_on_the_inverse_of_load():
    assert transform(50.0, "inverse") == 2.0
    assert transform(0.0, "inverse") == 100.0          # floored, never a division by zero
    assert SPECS["UP-2"].drivers[0].transform == "inverse"


def _ups(runtime, load, now):
    dev = DeviceView(device_id="u1", name="UPSA", device_type="ups", online=True)
    for key, v in (("battery_runtime", runtime), ("load_pct", load)):
        dev.num[(key, "")] = [Sample(now - 60 * i, v) for i in range(40)][::-1]
        dev.fresh.add((key, ""))
    return dev


def _up2(runtime, load):
    now = datetime(2026, 11, 3, 10, 0, tzinfo=UTC).timestamp()
    # runtime = 4 * (100 / load): 8 min at 50 %, learned between 25 % and 80 %
    base = {"status": "ok", "model": {"a": 0.0, "b": [4.0], "sigma": 0.3,
                                      "x_lo": [1.25], "x_hi": [4.0]}}
    st = DwellState()
    ri = RuleInput("UP-2", seed_params("UP-2"), _ups(runtime, load, now), None, now,
                   active=lambda k: False, x=lambda k: st.x, baseline=lambda k: base)
    (v,) = residual.run_rule(ri)
    return v


def test_a_battery_giving_its_usual_runtime_is_quiet():
    v = _up2(8.0, 50.0)
    assert v.cond is False and abs(v.expected - 8.0) < 1e-6


def test_a_fading_battery_counts_and_more_runtime_does_not():
    assert _up2(5.5, 50.0).cond is True                 # 2.5 min short at the same load
    assert _up2(10.5, 50.0).cond is False               # longer than usual: not a fault


def test_the_battery_is_judged_against_its_rooms_air():
    now = datetime(2026, 11, 3, 10, 0, tzinfo=UTC).timestamp()
    dev = DeviceView(device_id="u1", name="UPSA", device_type="ups", room_id="ur",
                     online=True)
    dev.num[("battery_temperature", "")] = [Sample(now - 60 * i, 31.0) for i in range(40)][::-1]
    dev.fresh.add(("battery_temperature", ""))
    base = {"status": "ok", "model": {"of": "battery_minus_room_ambient",
                                      "hod": {"10": [3.0, 0.3, 20]}}}
    ri = RuleInput("UP-1", seed_params("UP-1"), dev, Context(room_ambient={"ur": 24.0}), now,
                   active=lambda k: False, x=lambda k: {}, baseline=lambda k: base)
    (v,) = residual.run_rule(ri)
    assert v.cond is True and "7.0 K above the room's air" in v.summary


def test_every_seeded_l3_row_has_a_spec_and_step_3_ships_off():
    from tests.anomaly_fakes import seed_rows
    l3 = {r["id"]: r for r in seed_rows() if r["layer"] == 3}
    assert set(l3) == set(SPECS)
    assert {i for i, r in l3.items() if not r["enabled"]} == {"UP-1", "UP-2", "SV-1", "SV-2",
                                                              "NW-1"}
