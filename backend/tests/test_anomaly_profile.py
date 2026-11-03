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
