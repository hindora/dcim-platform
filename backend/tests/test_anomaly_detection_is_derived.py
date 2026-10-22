"""Every L2 raise is detection='derived', and none passes a state metric_key.

`detection_for` returns 'state' whenever metric_key is alarm_state or
equipment_state (core/alert_taxonomy.py; docs/29 D14). CH-4 reads
equipment_state and PU-3 reads Run_Status, so both must raise with a numeric
metric or None. Checked two ways: statically over every rule's verdict, and
through the sweep's raise path.
"""

from __future__ import annotations

import pytest

from app.analytics.detectors import rules as rl
from app.core import alert_taxonomy as tax
from tests.anomaly_fakes import FakeDB, FakeRedis, seed_rows, sweep_once

T0 = 1_800_000_000.0
STATE_KEYS = ("equipment_state", "alarm_state")
L2 = [r for r in seed_rows() if r["layer"] == 2]


def _verdicts_for(detector_id: str) -> list[rl.Verdict]:
    """Drive the rule with an empty view: every branch still names its key."""
    dv = rl.DeviceView(device_id="d", name="n", device_type="x")
    dv.num[("inlet_temperature", "")] = []
    ri = rl.RuleInput(detector_id, next(r for r in L2 if r["id"] == detector_id)["params"],
                      dv, rl.Context(), T0)
    return rl.run_rule(ri)


@pytest.mark.parametrize("row", L2, ids=[r["id"] for r in L2])
def test_no_rule_names_a_state_key_for_its_alarm(row):
    assert row["id"] in rl.RULES
    for v in _verdicts_for(row["id"]):
        assert v.alarm_metric_key not in STATE_KEYS
        assert tax.detection_for("analytics", metric_key=v.alarm_metric_key,
                                 alarm_type=row["alarm_type"]) == tax.DERIVED


@pytest.mark.parametrize("row", L2, ids=[r["id"] for r in L2])
def test_no_alarm_type_is_a_threshold_crossing(row):
    # A type listed in THRESHOLD_CROSSINGS would be filed `threshold`.
    assert row["alarm_type"] not in tax.THRESHOLD_CROSSINGS


async def test_the_sweep_raises_derived_alarms_only(monkeypatch):
    db = FakeDB()
    db.patch(monkeypatch)
    ch = db.add_device("chiller", "CH-1")
    for k in range(4):                         # short cycling: CH-4
        db.state(ch, "equipment_state", T0 + 600 * k, False, "Chiller_Running")
        db.state(ch, "equipment_state", T0 + 600 * k + 300, True, "Chiller_Running")
    db.state(ch, "equipment_state", T0 - 100, True, "Chiller_Running")
    pmp = db.add_device("pump", "PMP-1")         # proof lost: PU-3
    db.state(pmp, "equipment_state", T0, False, "Run_Status")
    for k in range(0, 2400, 30):
        db.sample(pmp, "pump_speed_pct", T0 + k, 80.0)
        db.sample(pmp, "water_flow", T0 + k, 20.0)
    redis = FakeRedis()
    for i in range(41):
        await sweep_once(db, T0 + 60 * i, redis)
    types = {r["alarm_type"] for r in db.raises}
    assert {"chiller_short_cycling", "pump_failed_to_prove"} <= types
    for r in db.raises:
        assert r["source"] == "analytics"
        assert r["metric_key"] not in STATE_KEYS
        assert tax.detection_for(r["source"], metric_key=r["metric_key"],
                                 alarm_type=r["alarm_type"]) == tax.DERIVED
