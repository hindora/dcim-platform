"""Forecasting, and mostly its refusals.

A forecast that is merely inaccurate gets corrected by the next week of data.
A forecast that is confidently wrong about WHEN capacity runs out gets put in a
budget. These tests are aimed at the second kind.
"""

from __future__ import annotations

import random

from app.services import forecast as f


def ramp(n: int, start: float = 100.0, per_day: float = 2.0) -> list[float]:
    return [start + per_day * i for i in range(n)]


def weekly(n: int, per_day: float = 0.8, noise: float = 0.0,
           seed: int = 7) -> list[float]:
    """A working week: flat Monday to Friday, a real drop at the weekend."""
    rng = random.Random(seed)
    shape = [0.0, 0.0, 0.0, 0.0, 0.0, -15.0, -18.0]
    return [100.0 + per_day * i + shape[i % 7] + rng.gauss(0, noise)
            for i in range(n)]


# --- the refusal --------------------------------------------------------------

def test_below_two_weeks_there_is_no_forecast_at_all():
    """Not a wide interval, not a provisional value - nothing to screenshot."""
    r = f.project(ramp(13), horizon_days=30, capacity=200)
    assert r["method"] == f.INSUFFICIENT
    assert r["points"] == []
    assert r["trend_per_day"] is None
    assert r["runway"]["days"] is None


def test_the_refusal_says_how_much_history_is_needed():
    r = f.project(ramp(9), horizon_days=30)
    assert "9 days" in r["method_reason"]
    assert "14" in r["method_reason"]


def test_fourteen_days_is_enough_to_answer():
    r = f.project(ramp(14), horizon_days=7)
    assert r["method"] == f.LINEAR
    assert len(r["points"]) == 7


def test_an_empty_series_refuses_rather_than_dividing_by_zero():
    r = f.project([], horizon_days=30)
    assert r["method"] == f.INSUFFICIENT
    assert r["points"] == []


# --- the straight line --------------------------------------------------------

def test_linear_recovers_a_known_slope():
    m = f.fit_linear(ramp(30, per_day=2.0))
    assert abs(m.slope - 2.0) < 1e-9
    assert abs(m.r2 - 1.0) < 1e-9


def test_a_flat_series_reports_no_fit_rather_than_a_perfect_one():
    """1 - SSE/SST is 1 - 0/0 on a flat line. Reporting r2 = 1.0 for "the
    trend explains everything" about a series with nothing to explain reads as
    high confidence in a forecast that is really just the last value."""
    m = f.fit_linear([50.0] * 20)
    assert m.slope == 0.0
    assert m.r2 == 0.0


def test_the_interval_widens_with_distance():
    """A 90-day extrapolation must not look as solid as a 5-day one."""
    m = f.fit_linear(weekly(30, noise=2.0))
    near_lo, near_hi = m.interval(30)
    far_lo, far_hi = m.interval(120)
    assert (far_hi - far_lo) > (near_hi - near_lo)


def test_small_samples_use_t_not_the_normal_quantile():
    """At 12 degrees of freedom the normal 1.96 understates the interval by
    about 11% - the difference between "a year" and "nine months"."""
    assert f.t95(12) > 2.17
    assert f.t95(5) > 2.5
    assert 1.95 < f.t95(10_000) < 2.0


# --- seasonality has to earn its place ----------------------------------------

def test_a_seasonal_series_picks_the_seasonal_model():
    method, why = f.choose(weekly(56, noise=1.0))
    assert method == f.HOLT_WINTERS
    assert "held-back week" in why


def test_a_plain_trend_keeps_the_straight_line():
    """The seasonal model has three more parameters and a value per weekday,
    so it can bend towards anything. It only wins if it wins out of sample."""
    rng = random.Random(3)
    plain = [100.0 + 0.8 * i + rng.gauss(0, 1.0) for i in range(56)]
    method, _ = f.choose(plain)
    assert method == f.LINEAR


def test_seasonality_is_not_offered_below_four_cycles():
    """Two cycles fits each weekday index from two observations."""
    method, why = f.choose(weekly(20, noise=1.0))
    assert method == f.LINEAR
    assert "28 days" in why
    assert f.fit_holt_winters(weekly(20)) is None


def test_the_seasonal_forecast_repeats_the_right_day_of_the_week():
    """Off-by-one in the seasonal index puts the weekend dip on a Thursday,
    which still looks plausible on a chart and is wrong every time."""
    hw = f.fit_holt_winters(weekly(70, noise=0.0))
    assert hw is not None
    # History ends mid-cycle; day 7 ahead must land on the same phase as day 14.
    assert abs(hw.predict(7) - hw.predict(14) + 7 * hw.trend) < 1.0


# --- step changes -------------------------------------------------------------

def test_a_step_change_is_found():
    step = ramp(20, per_day=0.5) + [180.0 + 0.5 * i for i in range(20)]
    assert f.detect_level_shift(step) == 20


def test_growth_alone_is_not_a_step():
    assert f.detect_level_shift(ramp(40, per_day=2.0)) is None
    assert f.detect_level_shift(weekly(40, noise=1.0)) is None


def test_the_fit_ignores_history_from_before_the_step():
    """A line across a step under-forecasts before it and over-forecasts after.

    Here the true growth is 0.5/day on both sides; fitted across the 80 kW
    jump it reads about 4.5 - nine times too steep, and every runway date
    derived from it is wrong.
    """
    step = ramp(20, per_day=0.5) + [180.0 + 0.5 * i for i in range(20)]
    r = f.project(step, horizon_days=10, capacity=300, unit="kW")
    assert abs(r["trend_per_day"] - 0.5) < 0.2
    assert any("step change" in n for n in r["notes"])


def test_a_step_too_recent_to_refit_on_refuses_rather_than_fitting_three_points():
    step = [*ramp(30, per_day=0.5), 200.0, 200.5, 201.0]
    r = f.project(step, horizon_days=10, capacity=300)
    # Either the shift is judged too close to the end to act on, or it is acted
    # on and there is too little left; both are honest, a 3-point fit is not.
    assert r["method"] in (f.LINEAR, f.INSUFFICIENT)
    if r["method"] == f.INSUFFICIENT:
        assert r["points"] == []


# --- horizon ------------------------------------------------------------------

def test_the_horizon_cannot_exceed_the_history():
    """A year off a month is not a longer forecast, it is the same forecast
    with the error bars off the page."""
    r = f.project(ramp(20), horizon_days=365)
    assert len(r["points"]) == 20
    assert any("shortened" in n for n in r["notes"])


# --- runway -------------------------------------------------------------------

def test_runway_is_reported_as_a_window_not_a_date():
    r = f.project(weekly(40, per_day=2.0, noise=1.0), horizon_days=40,
                  capacity=200.0, unit="kW")
    rw = r["runway"]
    assert rw["days"] is not None
    # The upper bound crosses first; that is the number a risk register wants.
    assert rw["earliest_days"] <= rw["days"]
    if rw["latest_days"] is not None:
        assert rw["latest_days"] >= rw["days"]


def test_no_capacity_means_no_runway_rather_than_infinite_runway():
    """This fleet records no rack, PDU or RPP rating. Silence is the honest
    answer; "never" would be a claim."""
    r = f.project(ramp(20), horizon_days=10, capacity=None)
    assert r["runway"]["days"] is None
    assert "no capacity limit" in r["runway"]["reason"]


def test_a_flat_series_never_crosses_and_says_so():
    r = f.project([100.0] * 20, horizon_days=20, capacity=500.0)
    assert r["runway"]["days"] is None
    assert "stays below" in r["runway"]["reason"]


def test_a_weak_trend_is_labelled_weak():
    rng = random.Random(11)
    noisy = [100.0 + rng.gauss(0, 20.0) for _ in range(30)]
    r = f.project(noisy, horizon_days=10, capacity=None)
    assert r["r2"] < 0.3
    assert any("weak evidence" in n for n in r["notes"])


# --- the route's wiring -------------------------------------------------------
#
# The service is exercised above on series it is handed. These check the parts
# only the endpoint does: picking the device set the metric actually means,
# refusing an unknown scope, and passing the data-quality notes through. The
# live fleet has about eleven hours of collection, so the fitted path cannot be
# reached against the real database yet - that is what the fourteen-day gate is
# for, and it is why this is stubbed rather than skipped.

import pytest  # noqa: E402

from app.api.v1 import analytics  # noqa: E402
from app.repositories import capacity as cap_repo  # noqa: E402
from app.repositories import forecast as forecast_repo  # noqa: E402
from app.services import capacity as capacity_service  # noqa: E402

FAKE_ID = "11111111-2222-3333-4444-555555555555"


def _stub_repos(monkeypatch, *, series_days: int, name: str | None = "Hall A",
                dropped: int = 0, gaps: int = 0) -> dict:
    """Stand in for the database, and record what the endpoint asked it for."""
    from datetime import UTC, datetime, timedelta
    seen: dict = {}

    async def scope_name(session, *, scope, scope_id):
        return name

    async def metric_id(session, key):
        seen["metric_key"] = key
        return 1

    async def devices_in_scope(session, *, scope, scope_id, device_types=None):
        seen["types"] = device_types
        return ["d1", "d2"]

    async def daily_power(session, *, device_ids, metric_id, days,
                          percentile=95):
        seen["history_days"] = days
        start = datetime(2026, 1, 1, tzinfo=UTC)
        vals = weekly(series_days, per_day=2.0, noise=1.0)
        return {
            "series": [{"day": start + timedelta(days=i), "p95_kw": v,
                        "peak_kw": v + 3, "mean_kw": v - 5, "minutes": 700,
                        "hours": 24}
                       for i, v in enumerate(vals)],
            "dropped_days": dropped, "gap_days": gaps,
        }

    monkeypatch.setattr(cap_repo, "scope_name", scope_name)
    monkeypatch.setattr(cap_repo, "metric_id", metric_id)
    monkeypatch.setattr(cap_repo, "devices_in_scope", devices_in_scope)
    monkeypatch.setattr(forecast_repo, "daily_power", daily_power)
    return seen


async def _call(**kw):
    from uuid import UUID
    params = {"scope": "room", "scope_id": UUID(FAKE_ID), "metric": "power",
              "horizon_days": 30, "history_days": 90, "capacity": None,
              "session": object(), "_": None}
    params.update(kw)
    return await analytics.forecast(**params)


@pytest.mark.asyncio
async def test_the_route_forecasts_when_there_is_enough_history(monkeypatch):
    _stub_repos(monkeypatch, series_days=60)
    r = await _call(capacity=250.0)
    assert r["method"] in (f.LINEAR, f.HOLT_WINTERS)
    assert len(r["points"]) == 30
    assert r["runway"]["days"] is not None
    assert r["name"] == "Hall A"


@pytest.mark.asyncio
async def test_the_route_refuses_a_short_series_through_the_full_stack(
        monkeypatch):
    _stub_repos(monkeypatch, series_days=9)
    r = await _call()
    assert r["method"] == f.INSUFFICIENT
    assert r["points"] == []
    assert r["history"]  # the history is still returned; only the fit is not


@pytest.mark.asyncio
async def test_it_power_asks_for_it_devices_only(monkeypatch):
    """Cooling load is what the plant must remove. Counting the chillers and
    CRAH fans as a load on themselves inflates the heat by about a third."""
    seen = _stub_repos(monkeypatch, series_days=60)
    await _call(metric="it_power")
    assert seen["types"] == capacity_service.IT_TYPES
    assert "crah" not in seen["types"]


@pytest.mark.asyncio
async def test_power_asks_for_every_end_load(monkeypatch):
    seen = _stub_repos(monkeypatch, series_days=60)
    await _call(metric="power")
    assert seen["types"] == capacity_service.LOAD_TYPES
    # Distribution gear is a conduit, not a load - a UPS reports the power
    # flowing THROUGH it to the racks.
    assert "ups" not in seen["types"]


@pytest.mark.asyncio
async def test_an_unknown_room_is_a_404_not_an_empty_forecast(monkeypatch):
    from fastapi import HTTPException
    _stub_repos(monkeypatch, series_days=60, name=None)
    with pytest.raises(HTTPException) as e:
        await _call()
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_dropped_days_and_gaps_reach_the_caller(monkeypatch):
    """Silently fitting through a collector outage reports growth that is
    really just the days that happened to be recorded."""
    _stub_repos(monkeypatch, series_days=60, dropped=2, gaps=3)
    r = await _call()
    assert any("dropped" in n for n in r["notes"])
    assert any("missing from the middle" in n for n in r["notes"])
