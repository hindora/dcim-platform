"""Which aggregate a history window is served from.

Getting this wrong is not an error, it is a slow chart nobody can read - so it
is worth pinning the arithmetic rather than trusting the tiers to stay sane.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.repositories.telemetry import (
    MAX_ROWS,
    TARGET_POINTS_PER_SERIES,
    choose_source,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
BUCKET_SECONDS = {"1m": 60, "5m": 300, "1h": 3600, "1d": 86400}


def route(**kw) -> str:
    return choose_source(NOW - timedelta(**kw), NOW)[2]


# --- the ladder --------------------------------------------------------------

@pytest.mark.parametrize("window,expected", [
    ({"hours": 1}, "1m"),      # an hour of minutes is 60 points - keep the detail
    ({"hours": 6}, "5m"),
    ({"days": 1}, "1h"),       # a day reads as its hourly shape, not 1440 samples
    ({"days": 7}, "1h"),
    ({"days": 30}, "1d"),      # 30 points, not 720
    ({"days": 90}, "1d"),
])
def test_window_routes_to_the_right_aggregate(window, expected):
    assert route(**window) == expected


def test_a_month_is_served_from_daily_buckets():
    """30 days at 5m is 8,640 points per line, and at 1h it is still 720.

    Slow to ship, and once drawn there are more points than pixels. A month
    view wants 30 of them.
    """
    assert route(days=30) == "1d"


def test_a_day_is_not_served_from_minute_buckets():
    """The regression this budget exists to prevent.

    A day at 1m is 1,440 points into a plot ~650 units wide, so a noisy signal
    overprints into a band and the trajectory is lost inside it. Worse, it made
    a WEEK less dense than a DAY - the week fell through to 5m and drew 326
    points where the day drew 1,440. Hourly buckets read as the daily shape.
    """
    assert route(days=1) == "1h"
    assert route(hours=6) == "5m"


def test_every_auto_route_stays_inside_the_per_series_budget():
    """Now true for every window the charts offer.

    It was not before the daily aggregate existed: the ladder stopped at 1h, so
    30 days routed to 720 points per series and 90 days to 2,160, and this test
    had to carry an explicit exemption for "already as coarse as the data goes".
    The exemption is gone because the case is.
    """
    for days in (1, 2, 7, 14, 30, 60, 90, 180):
        label = route(days=days)
        points = days * 86400 / BUCKET_SECONDS[label]
        assert points <= TARGET_POINTS_PER_SERIES, (
            f"{days}d routed to {label}, which is {points:.0f} points per series")

def test_the_finest_bucket_that_fits_is_chosen():
    """Not merely a bucket that fits - the most detailed one that does."""
    for days in (1, 7, 30):
        label = route(days=days)
        finer = {"5m": "1m", "1h": "5m"}.get(label)
        if finer:
            points = days * 86400 / BUCKET_SECONDS[finer]
            assert points > TARGET_POINTS_PER_SERIES, (
                f"{days}d could have used {finer} and did not")


# --- explicit overrides ------------------------------------------------------

def test_raw_is_only_reachable_by_asking_for_it():
    """Its density depends on the poll interval, so it is never auto-routed to."""
    assert choose_source(NOW - timedelta(minutes=5), NOW)[0] != "telemetry_sample"
    assert choose_source(NOW - timedelta(days=1), NOW, "raw")[0] == "telemetry_sample"


@pytest.mark.parametrize("interval", ["1m", "5m", "1h", "1d"])
def test_an_explicit_interval_is_honoured_over_the_budget(interval):
    table, _, label = choose_source(NOW - timedelta(days=365), NOW, interval)
    assert label == interval
    assert table == f"telemetry_{interval}"


def test_a_very_long_window_still_answers_with_the_coarsest_bucket():
    """More points than the budget beats an error or an empty chart."""
    assert route(days=3650) == "1d"


# --- row cap -----------------------------------------------------------------

def test_the_row_cap_is_not_the_per_series_budget():
    """They are different questions.

    One line wants 2500 points; a seven-metric chart legitimately wants seven
    times that. Using one constant for both truncated multi-series charts.
    """
    assert MAX_ROWS > TARGET_POINTS_PER_SERIES


def test_a_typical_multi_metric_chart_fits_under_the_cap():
    # Seven metrics over a month: 7 * 720 hourly points.
    assert 7 * (30 * 86400 / BUCKET_SECONDS["1h"]) <= MAX_ROWS
