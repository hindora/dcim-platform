"""Thermal classification.

The costly mistakes here are diagnostic, not arithmetic: sending an engineer to
the wrong end of the building, or reporting a quiet room during a real event.
"""

from __future__ import annotations

from app.services import thermal as t


def crah(supply=22.0, ret=27.0, setpoint=22.0, running=True) -> t.CrahThermal:
    return t.CrahThermal(device_id="x", name="CRAH1", supply_c=supply,
                         return_c=ret, setpoint_c=setpoint, running=running)


def rack(name: str, inlet: float, exhaust: float | None = None) -> t.RackThermal:
    return t.RackThermal(rack_id=name, name=name, inlet_mean=inlet,
                         inlet_min=inlet, inlet_max=inlet,
                         exhaust_mean=exhaust, samples=30)


# --- the distinction this exists for -----------------------------------------

def test_a_unit_that_cannot_hold_its_discharge_is_a_unit_fault():
    """High supply means it is not cooling. The fix is at the machine."""
    state, why = t.classify_crah(crah(supply=28.0, ret=30.0, setpoint=22.0), 30.0)
    assert state == "high_supply"
    assert "not cooling" in why and "not the floor" in why


def test_a_unit_taking_hot_air_is_a_load_problem_not_a_unit_fault():
    """High return with a good supply means the room is feeding it hot air."""
    state, why = t.classify_crah(crah(supply=22.0, ret=36.0), 30.0)
    assert state == "high_return"
    assert "not a unit fault" in why


def test_supply_wins_when_both_are_high():
    """A unit that is not delivering cold air is broken whatever its return
    reads; the hot return is then a consequence, not a second finding."""
    state, _ = t.classify_crah(crah(supply=30.0, ret=40.0, setpoint=22.0), 30.0)
    assert state == "high_supply"


def test_a_stopped_unit_is_not_graded_as_healthy():
    """Its last supply reading is whatever it was delivering when it stopped.

    Grading that against setpoint reported five stopped CRAHs as "ok" in the
    middle of a real room event.
    """
    state, why = t.classify_crah(crah(running=False), 30.0)
    assert state == "stopped"
    assert "stale" in why


def test_a_healthy_unit_is_ok():
    assert t.classify_crah(crah(), 30.0)[0] == "ok"


def test_high_return_also_fires_without_a_peer_to_compare_against():
    """When EVERY unit is taking hot air, none of them is relatively hot.

    The relative test alone went quiet during a room-wide shortfall, which is
    the case where the reading matters most.
    """
    state, why = t.classify_crah(crah(ret=34.6), room_return_p90=34.5)
    assert state == "high_return"
    assert "absolute" in why


# --- hot spots ---------------------------------------------------------------

def test_a_rack_hotter_than_its_neighbours_is_a_hot_spot():
    racks = [rack("A", 22.0), rack("B", 22.5), rack("C", 27.0)]
    spots = t.hot_spots(racks, p90=22.5)
    assert [s["name"] for s in spots] == ["C"]


def test_sustained_means_the_minimum_was_over_the_line():
    """A mean can be dragged over the threshold by one spike; the minimum
    cannot. Otherwise the detector fires on a fan ramp."""
    spiky = t.RackThermal(rack_id="D", name="D", inlet_mean=27.0, inlet_min=21.0,
                          inlet_max=40.0, samples=30)
    assert t.hot_spots([spiky], p90=22.5) == []


def test_a_uniformly_warm_room_has_no_hot_spot():
    """Correct, and the reason the room-wide event exists: when everything
    rises together the percentile rises with it."""
    racks = [rack(n, 31.0) for n in ("A", "B", "C")]
    assert t.hot_spots(racks, p90=31.0) == []


def test_no_baseline_means_no_hot_spots_rather_than_all_of_them():
    assert t.hot_spots([rack("A", 30.0)], p90=None) == []


# --- rack airflow ------------------------------------------------------------

def test_rack_delta_t_is_exhaust_minus_intake():
    assert rack("A", 23.0, 34.0).delta_t_k == 11.0


def test_ashrae_envelope_flags():
    assert rack("A", 24.0).above_recommended is False
    assert rack("B", 29.0).above_recommended is True
    assert rack("C", 33.0).above_allowable is True


# --- percentile baseline -----------------------------------------------------

def test_percentile_uses_nearest_rank_not_bankers_rounding():
    """The baseline every hot-spot threshold is measured from.

    round() breaks ties to even, so an exact rank of 3.0 picked the 4th value
    while 2.0 picked the 2nd - a whole rack of drift on a 20-rack room.
    """
    ten = [float(i) for i in range(1, 11)]
    assert t.percentile(ten, 90) == 9.0        # ceil(9.0) -> 9th
    assert t.percentile(ten, 50) == 5.0
    assert t.percentile([float(i) for i in range(1, 6)], 90) == 5.0


def test_percentile_survives_degenerate_input():
    assert t.percentile([], 90) is None
    assert t.percentile([7.0], 90) == 7.0
