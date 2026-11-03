"""The expected band and learning progress the panel and charts show (docs/35 §7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.analytics import expected

PARAMS = {"z_open": 4.0, "sigma_floor": 2.0, "min_hour_samples": 8, "min_hours": 120,
          "min_days": 10}
NOW = datetime(2026, 11, 3, 10, 30, tzinfo=UTC)


def _row(status="ok", hod=None, hours=150, days=12):
    return {"status": status, "fitted_at": NOW,
            "model": {"hod": hod if hod is not None else {"10": [50.0, 3.0, 20],
                                                        "11": [55.0, 1.0, 20],
                                                        "12": [60.0, 3.0, 4]},
                      "hours": hours, "days": days}}


def test_the_band_now_is_the_detectors_own_distance():
    v = expected.view(_row(), PARAMS, NOW)
    assert v["now"] == {"hour": 10, "expected": 50.0, "lo": 38.0, "hi": 62.0}
    assert v["learning"] is False


def test_the_spread_floor_widens_a_too_quiet_hour():
    v = expected.view(_row(), PARAMS, NOW.replace(hour=11))
    assert v["now"]["lo"] == 55.0 - 4 * 2.0          # sigma 1.0 floored to 2.0


def test_learning_progress_is_reported():
    v = expected.view(_row(status="thin", hours=93, days=17), PARAMS, NOW)
    assert v["learning"] is True and (v["hours"], v["min_hours"]) == (93, 120)


def test_an_hour_with_too_few_samples_has_no_band_now():
    v = expected.view(_row(), PARAMS, NOW.replace(hour=12))
    assert v["now"] is None


def test_the_band_covers_only_learned_hours_of_the_window():
    start, end = NOW.replace(hour=9, minute=0), NOW.replace(hour=13, minute=0)
    v = expected.view(_row(), PARAMS, NOW, window=(start, end))
    hours = {datetime.fromtimestamp(p[0] / 1000, UTC).hour for p in v["band"]}
    assert hours == {10, 11}                          # 09 unseen, 12 too thin
    assert all(p[1] <= p[2] <= p[3] for p in v["band"])


def test_the_band_is_clipped_to_the_window():
    start = NOW.replace(minute=15)
    v = expected.view(_row(), PARAMS, NOW, window=(start, start + timedelta(minutes=30)))
    ts = [p[0] for p in v["band"]]
    assert min(ts) == int(start.timestamp() * 1000)
    assert max(ts) < int((start + timedelta(minutes=30)).timestamp() * 1000)
