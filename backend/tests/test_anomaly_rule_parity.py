"""The display verdict and the derived alert share ONE constant each.

classify_crah / CR-1, plant's valve verdict / VA-1, classify_cdu / CD-1 and
hot_spots / RM-3 (docs/33 task 1.4, docs/29 D18). The constants live in
analytics/catalogue.py and services/thermal.py and services/plant.py import
them back; the 0112 seeds carry the same values. Behaviour is checked at the
boundary as well, so a strict `>` cannot quietly become `>=` on one side.
"""

from __future__ import annotations

import pytest

from app.analytics import catalogue as cat
from app.analytics.detectors import rules as rl
from app.services import plant, thermal
from tests.anomaly_fakes import flat, running, seed_params, view

T = 1_800_000_000.0


def test_one_constant_each():
    assert thermal.SUPPLY_FAULT_MARGIN_K is cat.CRAH_SUPPLY_FAULT_MARGIN_K
    assert thermal.CDU_SUPPLY_FAULT_MARGIN_K is cat.CDU_SUPPLY_FAULT_MARGIN_K
    assert thermal.HOT_SPOT_MARGIN_K is cat.HOT_SPOT_MARGIN_K
    assert plant.VALVE_DEVIATION_PCT is cat.VALVE_DEVIATION_PCT


def test_the_seeds_carry_the_shared_values():
    assert seed_params("CR-1")["margin_k"] == cat.CRAH_SUPPLY_FAULT_MARGIN_K
    assert seed_params("CD-1")["over_setpoint_k"] == cat.CDU_SUPPLY_FAULT_MARGIN_K == 2.0
    assert seed_params("VA-1")["deviation_pct"] == cat.VALVE_DEVIATION_PCT
    assert seed_params("RM-3")["margin_k"] == cat.HOT_SPOT_MARGIN_K
    assert seed_params("RM-2")["max_rate_k_per_h"] == cat.MAX_RATE_K_PER_H


def _cr1(supply, setpoint):
    v = view("crah")
    v.num[("supply_air_temp", "")] = flat(supply, T)
    v.num[("air_setpoint_temp", "")] = flat(setpoint, T)
    v.num[("valve_position_pct", "CHW")] = flat(100.0, T)
    v.bools[("equipment_state", "Unit_Running")] = running(T, T - 7200)
    return rl.cr1(rl.RuleInput("CR-1", seed_params("CR-1"), v, rl.Context(), T))[0]


@pytest.mark.parametrize("over", [2.9, 3.1, 5.0])
def test_crah_display_and_cr1_agree(over):
    unit = thermal.CrahThermal(device_id="d", name="n", supply_c=20.0 + over,
                               return_c=32.0, setpoint_c=20.0, running=True)
    shown = thermal.classify_crah(unit, None)[0] == "high_supply"
    assert shown == bool(_cr1(20.0 + over, 20.0).cond)


def _cd1(supply, setpoint):
    v = view("cdu")
    v.num[("water_supply_temp", "")] = flat(supply, T)
    v.num[("water_setpoint_temp", "")] = flat(setpoint, T)
    v.num[("valve_position_pct", "FACILITY_CHW")] = flat(100.0, T)
    v.bools[("equipment_state", "Unit_Running")] = running(T, T - 7200)
    return rl.cd1(rl.RuleInput("CD-1", seed_params("CD-1"), v, rl.Context(), T))[0]


@pytest.mark.parametrize("over", [1.9, 2.1, 4.0])
def test_cdu_display_and_cd1_agree(over):
    unit = thermal.CduThermal(device_id="d", name="n", supply_c=27.0 + over,
                              setpoint_c=27.0, valve_pct=100.0, running=True)
    shown = thermal.classify_cdu(unit)[0] == "high_supply"
    assert shown == bool(_cd1(27.0 + over, 27.0).cond)


def _va1(cmd, meas):
    v = view("valve")
    v.num[("valve_position_pct", "MEASURED")] = flat(meas, T)
    v.num[("valve_position_pct", "COMMANDED")] = flat(cmd, T)
    return rl.va1(rl.RuleInput("VA-1", seed_params("VA-1"), v, rl.Context(), T))[0]


@pytest.mark.parametrize("gap", [9.5, 10.5, 30.0])
def test_plant_valve_verdict_and_va1_agree(gap):
    shown = gap > plant.VALVE_DEVIATION_PCT       # plant.py's verdict line, verbatim
    assert shown == bool(_va1(60.0, 60.0 - gap).cond)


def test_rm3_uses_the_room_pages_percentile():
    vals = [21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0]
    assert rl.room_p90(vals) == thermal.percentile(vals, 90)
