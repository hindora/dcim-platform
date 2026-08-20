"""Cooling arithmetic and staging.

Every number here is a physical claim, so the tests are about physics as much
as about code.
"""

from __future__ import annotations

import pytest

from app.services import cooling as c


def chiller(name: str, running: bool, rated: float | None,
            supply: float | None = 7.0, ret: float | None = 12.0,
            flow: float | None = 5.4, cop: float | None = None,
            power: float | None = None) -> c.Chiller:
    return c.Chiller(device_id=name, name=name, running=running, rated_kw=rated,
                     cop=cop, power_kw=power,
                     chw=c.Loop("CHW", supply, ret, flow))


# --- the equation ------------------------------------------------------------

def test_heat_is_flow_times_delta_t_times_cp():
    """5.4 L/s across 5 K of water is about 113 kW.

    Checked against the plant itself: that chiller reports COP 3.97 on 28.4 kW
    input, which is 113 kW of cooling by a completely independent route.
    """
    assert c.heat_kw(5.4, 5.0) == pytest.approx(113.0, abs=0.5)


def test_delta_t_is_signed():
    """A return colder than the supply is a swapped sensor pair or reversed
    flow. Clamping it to zero would hide a real fault as a healthy loop."""
    assert c.delta_t(12.0, 7.0) == -5.0


def test_missing_readings_give_no_answer_rather_than_zero():
    """Zero heat and unknown heat are different claims."""
    assert c.heat_kw(None, 5.0) is None
    assert c.heat_kw(5.0, None) is None
    assert c.delta_t(None, 12.0) is None


def test_low_delta_t_is_flagged():
    """The classic chilled-water pathology: plenty of flow, no heat transfer."""
    assert c.Loop("CHW", 7.0, 9.0, 5.0).low_delta_t is True
    assert c.Loop("CHW", 7.0, 12.0, 5.0).low_delta_t is False


def test_a_stopped_loop_is_not_low_delta_t():
    """It reads 0 K because nothing is circulating, not because heat transfer
    failed. Four standby units on a fault list teaches people to ignore it."""
    assert c.Loop("CHW", 7.0, 7.0, 0.0).low_delta_t is False
    assert c.Loop("CHW", 7.0, 7.0, None).low_delta_t is False


# --- the two independent estimates -------------------------------------------

def test_the_two_output_estimates_agree_on_good_data():
    ch = chiller("CHL1", True, 800.0, cop=3.97, power=28.4)
    assert ch.output_thermal_kw == pytest.approx(113.0, abs=1.0)
    assert ch.output_electrical_kw == pytest.approx(112.8, abs=1.0)
    assert ch.output_disagreement_pct < c.OUTPUT_AGREEMENT_PCT


def test_disagreement_is_reported_not_averaged():
    """Averaging a bad sensor with a good one produces a number that is wrong
    and looks reasonable, which is worse than either input."""
    ch = chiller("CHL1", True, 800.0, flow=20.0, cop=3.97, power=28.4)
    plant = c.PlantView(chillers=[ch])
    notes = c.data_quality(plant)
    assert any("apart" in n for n in notes)


def test_a_reversed_loop_is_called_out():
    ch = chiller("CHL1", True, 800.0, supply=12.0, ret=7.0)
    assert any("colder than supply" in n for n in c.data_quality(c.PlantView(chillers=[ch])))


# --- staging -----------------------------------------------------------------

def test_two_running_machines_carrying_a_light_load_are_n_plus_1():
    plant = c.PlantView(chillers=[chiller("A", True, 800.0),
                                  chiller("B", True, 800.0)])
    kind, why = c.staging_verdict(plant)
    assert kind == "N+1"
    assert "still leaves" in why


def test_one_running_machine_is_not_redundant():
    plant = c.PlantView(chillers=[chiller("A", True, 800.0),
                                  chiller("B", False, 800.0)])
    kind, why = c.staging_verdict(plant)
    assert kind == "N"
    assert "standby machine(s) available to start" in why


def test_redundancy_is_judged_on_what_is_already_running():
    """A standby machine has to start, pull down and stage on. It does not help
    in the minutes after a trip, so it is counted separately, not as capacity."""
    # 20 L/s across 5 K is ~419 kW: comfortably inside one 800 kW machine, so
    # the plant carries it - but losing that machine leaves nothing, and the
    # 800 kW sitting on standby does not change that.
    plant = c.PlantView(chillers=[chiller("A", True, 800.0, flow=20.0),
                                  chiller("B", False, 800.0)])
    assert c.staging_verdict(plant)[0] == "N"


def test_load_beyond_the_running_capacity_is_over_capacity():
    plant = c.PlantView(chillers=[chiller("A", True, 100.0, flow=40.0)])
    kind, _ = c.staging_verdict(plant)
    assert kind == "over_capacity"


def test_a_plant_with_nothing_running_has_no_capacity():
    plant = c.PlantView(chillers=[chiller("A", False, 800.0)])
    assert c.staging_verdict(plant)[0] == "no_capacity"


def test_running_but_moving_no_heat_is_idle_not_redundant():
    """Pumps turning against a closed load is not the same as cooling."""
    plant = c.PlantView(chillers=[chiller("A", True, 800.0, ret=7.0)])
    assert c.staging_verdict(plant)[0] == "idle"


def test_standby_capacity_is_not_counted_as_load_carrying():
    plant = c.PlantView(chillers=[chiller("A", True, 800.0),
                                  chiller("B", False, 800.0)])
    assert plant.running_capacity_kw == 800.0
    assert plant.installed_capacity_kw == 1600.0
