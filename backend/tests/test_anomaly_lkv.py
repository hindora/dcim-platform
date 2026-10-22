"""Change-logged booleans rebuilt as last-known values (docs/33 Phase 1).

Booleans are written on change plus a 900 s heartbeat (ingest/changelog.py).
CH-4 and PU-3 must rebuild run state from that: a heartbeat row repeats the
value and is NOT a start, the backfill's oldest row is not a start, and the
value is "known" only inside the 1800 s look-back.
"""

from __future__ import annotations

from app.analytics import window as w
from app.analytics.detectors import rules as rl
from tests.anomaly_fakes import FakeDB, FakeRedis, sweep_once

T0 = 1_800_000_000.0


def test_a_heartbeat_is_not_a_start():
    win, got = w.merge_bool(w.BoolWindow(), [(T0, True, "good"), (T0 + 900, True, "good"),
                                             (T0 + 1800, True, "good")], T0 + 1800)
    assert got and win.series.value is True
    assert win.series.starts == []
    assert win.series.since == T0 and win.series.since_known is False


def test_transitions_after_a_known_value_are_starts():
    rows = [(T0, False, "good"), (T0 + 60, True, "good"), (T0 + 300, False, "good"),
            (T0 + 360, True, "good"), (T0 + 900 + 360, True, "good")]
    win, _ = w.merge_bool(w.BoolWindow(), rows, T0 + 1300)
    assert win.series.starts == [T0 + 60, T0 + 360]
    assert win.series.since == T0 + 360 and win.series.since_known is True
    # A replay of rows at or before the cursor changes nothing.
    again, got = w.merge_bool(win, rows, T0 + 1300)
    assert not got and again.series.starts == [T0 + 60, T0 + 360]


def test_the_first_row_seen_is_not_a_start_even_if_it_is_true():
    win, _ = w.merge_bool(w.BoolWindow(), [(T0, True, "good")], T0)
    win, _ = w.merge_bool(win, [(T0 + 60, True, "good")], T0 + 60)
    assert win.series.starts == []


def test_a_bad_row_says_nothing():
    win, _ = w.merge_bool(w.BoolWindow(), [(T0, True, "good"), (T0 + 30, False, "bad")],
                          T0 + 30)
    assert win.series.value is True and win.cursor == T0 + 30


def test_last_known_expires_after_the_lookback():
    b = rl.BoolSeries(value=True, last_ts=T0, since=T0)
    assert b.known(T0 + 1799) is True
    assert b.known(T0 + 1801) is None


def test_the_wire_form_round_trips():
    win, _ = w.merge_bool(w.BoolWindow(), [(T0, False, "good"), (T0 + 60, True, "good")],
                          T0 + 60)
    back = w.parse(win.to_wire())
    assert isinstance(back, w.BoolWindow) and back.series == win.series


async def test_ch4_counts_only_real_starts_through_the_sweep(monkeypatch):
    db = FakeDB()
    db.patch(monkeypatch)
    dev = db.add_device("chiller", "CH-1")
    redis = FakeRedis()
    # Heartbeats every 900 s while running, then four short cycles.
    for k in range(4):
        db.state(dev, "equipment_state", T0 - 3600 + 900 * k, True, "Chiller_Running")
    t = T0
    for _ in range(4):
        db.state(dev, "equipment_state", t, False, "Chiller_Running")
        db.state(dev, "equipment_state", t + 300, True, "Chiller_Running")
        t += 600
    # Sweep before the cycles: heartbeats only, no starts, nothing opens.
    r = await sweep_once(db, T0 - 60, redis)
    assert r.findings_opened == 0
    for i, now in enumerate((T0 + 400, T0 + 1000, T0 + 1600, T0 + 2200)):
        r = await sweep_once(db, now, redis)
        if i < 3:
            assert r.findings_opened == 0, i      # 1, 2, 3 starts: not more than 3
    assert r.findings_opened == 1
    f = next(iter(db.findings.values()))
    assert f["detector_id"] == "CH-4" and f["observed"] == 4.0
    # The derived alarm carries no state metric_key (docs/29 D14).
    assert db.raises[-1]["metric_key"] is None


async def test_pu3_rebuilds_run_state_from_heartbeats(monkeypatch):
    db = FakeDB()
    db.patch(monkeypatch)
    dev = db.add_device("pump", "PMP-1")
    redis = FakeRedis()
    # Run_Status heartbeat says stopped; the pump is spinning at 80 %.
    db.state(dev, "equipment_state", T0 - 1000, False, "Run_Status")
    for k in range(0, 1200, 30):
        db.sample(dev, "pump_speed_pct", T0 - 1000 + k, 80.0)
        db.sample(dev, "water_flow", T0 - 1000 + k, 20.0)
    opened = 0
    for i in range(3):
        r = await sweep_once(db, T0 + 60 * i, redis)
        opened += r.findings_opened
    assert opened == 1
    f = next(iter(db.findings.values()))
    assert f["detector_id"] == "PU-3"
    assert db.raises[-1]["metric_key"] == "water_flow"
