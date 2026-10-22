"""No analytics raise exceeds MINOR while the cap is on (docs/33 task 1.18).

Under the default integration policy a MAJOR alarm-class alarm opens a Jira
ticket after 300 s; a MAJOR derived alarm raised before the L5 correlator
exists would ticket every symptom (docs/33 section 1 item 2).
"""

from __future__ import annotations

import pytest

from app.analytics import sweep as anomaly_sweep
from app.analytics.catalogue import DERIVED_SEVERITY_CAP
from tests.anomaly_fakes import FakeDB, FakeRedis, seed_catalogue, seed_rows, sweep_once

T0 = 1_800_000_000.0
RANK = {"INFO": 1, "WARNING": 2, "MINOR": 3, "MAJOR": 4, "CRITICAL": 5}


def test_the_cap_is_minor_and_every_seed_is_at_or_below_it():
    assert DERIVED_SEVERITY_CAP == "MINOR"
    for r in seed_rows():
        if r["severity"]:
            assert RANK[r["severity"]] <= RANK["MINOR"], r["id"]


@pytest.mark.parametrize("asked,got", [("CRITICAL", "MINOR"), ("MAJOR", "MINOR"),
                                       ("MINOR", "MINOR"), ("WARNING", "WARNING"),
                                       (None, "MINOR"), ("bogus", "MINOR")])
def test_derived_severity(asked, got):
    assert anomaly_sweep.derived_severity(asked) == got


async def test_a_row_that_asks_for_major_is_still_raised_minor(monkeypatch):
    # The DB CHECK refuses a MAJOR seed; this is the code-path guard behind it,
    # for a row that somehow says MAJOR (a hand edit, a future promotion path).
    db = FakeDB(seed_catalogue(**{"CR-1": {"severity": "MAJOR"},
                                  "CR-6": {"severity": "CRITICAL"}}))
    db.patch(monkeypatch)
    dev = db.add_device("crah", "CRAH-1")
    for k in range(0, 3000, 30):
        t = T0 - 1800 + k
        # Moving a little: a frozen supply with a moving valve is DQ-5's
        # flatline, which would (rightly) mask CR-1.
        db.sample(dev, "supply_air_temp", t, 26.0 + 0.1 * ((k // 30) % 3))
        db.sample(dev, "air_setpoint_temp", t, 20.0)
        db.sample(dev, "valve_position_pct", t, 100.0 if (k // 60) % 2 else 40.0, "CHW")
        db.sample(dev, "return_air_temp", t, 36.0)
        db.sample(dev, "fan_speed_pct", t, 60.0)
    for hb in range(4):                     # a 900 s heartbeat, as ingest writes
        db.state(dev, "equipment_state", T0 - 1700 + 900 * hb, True, "Unit_Running")
    redis = FakeRedis()
    for i in range(20):
        await sweep_once(db, T0 + 60 * i, redis)
    assert {r["alarm_type"] for r in db.raises} >= {"crah_supply_over_setpoint",
                                                     "crah_valve_hunting"}
    for r in db.raises:
        assert RANK[r["severity"]] <= RANK["MINOR"], r
