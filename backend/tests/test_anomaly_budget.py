"""The advisory budget: a queue people can read (docs/33 task 3.8, docs/35 §6)."""

from __future__ import annotations

from datetime import date, timedelta

from app.analytics import budget as b
from app.analytics import sweep as anomaly_sweep
from tests.anomaly_fakes import FakeDB, FakeRedis, seed_params, sweep_once

T0 = 1_800_000_000.0
DAY = date(2026, 11, 3)


# --- the decisions -------------------------------------------------------------

def test_a_detector_may_open_its_cap_and_no_more():
    bud = b.Budget(per_detector=3, estate_process=100, day=DAY)
    for _ in range(3):
        assert bud.allow("CR-2", 2, "advisory") is None
        bud.opened("CR-2", 2)
    assert bud.allow("CR-2", 2, "advisory") == "detector_daily_cap"
    assert bud.allow("CR-1", 2, "advisory") is None            # another detector's own cap


def test_process_findings_share_an_estate_budget_data_quality_does_not():
    bud = b.Budget(per_detector=100, estate_process=4, day=DAY)
    for did in ("CR-1", "CR-2", "SV-3", "TH-1"):
        bud.opened(did, 2 if did.startswith("CR") else 3)
    assert bud.allow("SV-4", 3, "advisory") == "estate_daily_cap"
    assert bud.allow("DQ-4", 0, "advisory") is None            # sensor noise cannot spend it


def test_the_alert_lane_is_never_rate_limited():
    bud = b.Budget(per_detector=1, estate_process=1, day=DAY)
    bud.opened("PU-3", 2)
    bud.opened("PU-3", 2)
    assert bud.allow("PU-3", 2, "alert") is None


def test_counts_already_stored_today_are_honoured():
    bud = b.Budget(per_detector=2, estate_process=100, day=DAY,
                   today={"CR-2": {"opened": 2, "held": 0}})
    assert bud.allow("CR-2", 2, "advisory") == "detector_daily_cap"


def test_two_days_held_back_switches_a_process_detector_off():
    rows = {("CR-2", DAY): {"opened": 10, "held": 4},
            ("CR-2", DAY - timedelta(days=1)): {"opened": 10, "held": 1}}
    assert b.to_switch_off(rows, "CR-2", 2, "advisory", DAY)
    assert not b.to_switch_off(rows, "CR-2", 2, "advisory", DAY + timedelta(days=1))


def test_one_bad_day_alarms_and_data_quality_are_never_switched_off():
    one_day = {("SV-3", DAY): {"opened": 10, "held": 9}}
    assert not b.to_switch_off(one_day, "SV-3", 3, "advisory", DAY)
    both = {("DQ-4", DAY): {"held": 5}, ("DQ-4", DAY - timedelta(days=1)): {"held": 5}}
    assert not b.to_switch_off(both, "DQ-4", 0, "advisory", DAY)   # it also masks
    alarm = {("GN-1", DAY): {"held": 5}, ("GN-1", DAY - timedelta(days=1)): {"held": 5}}
    assert not b.to_switch_off(alarm, "GN-1", 2, "alert", DAY)


def test_the_note_says_why_and_what_to_do():
    note = b.switch_off_note("CR-2", 10, DAY)
    assert "10 findings a day" in note and "Lower its sensitivity" in note


# --- through the sweep -----------------------------------------------------------

def _storm(db, n):
    for i in range(n):
        dev = db.add_device("sensor", f"SEN-{i}", poll_interval_s=60)
        for k in range(0, 1200, 60):
            db.sample(dev, "inlet_temperature", T0 - 1200 + k, 22.0)
        for k in range(0, 1800, 60):
            db.sample(dev, "relative_humidity", T0 - 1200 + k, 45.0 + (k % 120) / 60)


async def test_a_storm_opens_the_cap_and_counts_the_rest_once(monkeypatch):
    db = FakeDB()
    db.patch(monkeypatch)
    _storm(db, 25)
    redis = FakeRedis()                                         # persists, as Redis does
    r = await sweep_once(db, T0 + 600, redis)
    assert r.findings_opened == 10                              # DQ-1's daily cap
    day = next(iter(db.budget))[1]
    assert db.budget[("DQ-1", day)] == {"opened": 10, "held": 15}
    # The next sweep sees the same 15 still silent: they wait out their dwell
    # again rather than being counted again.
    await sweep_once(db, T0 + 660, redis)
    assert db.budget[("DQ-1", day)]["held"] == 15


async def test_two_days_over_budget_switches_the_detector_off_and_says_so(monkeypatch):
    db = FakeDB()
    db.patch(monkeypatch)
    today = anomaly_sweep.datetime.fromtimestamp(T0, anomaly_sweep.UTC).date()
    db.budget = {("SV-3", today): {"opened": 10, "held": 2},
                 ("SV-3", today - timedelta(days=1)): {"opened": 10, "held": 3}}
    await sweep_once(db, T0, FakeRedis())
    off = getattr(db, "switched_off", [])
    assert [d for d, _ in off] == ["SV-3"]
    assert "Switched off by the daily budget" in off[0][1]


def test_dq4_leaves_power_to_the_process():
    assert "power_draw" in seed_params("DQ-4")["exclude_metrics"]
