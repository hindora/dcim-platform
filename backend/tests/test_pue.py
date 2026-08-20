"""PUE plausibility and labelling.

The number is only meaningful with its method and category attached, so the
tests are as much about what the response says as about the arithmetic.
"""

from __future__ import annotations

from app.services import pue as p

# --- plausibility ------------------------------------------------------------

def test_a_normal_pue_is_plausible():
    for value in (1.2, 1.42, 1.6, 2.0):
        ok, note = p.classify(value)
        assert ok and note is None


def test_a_pue_below_one_is_refused_as_impossible():
    """Facility energy contains IT energy by definition.

    Below 1.0 is not an efficient site, it is a double-counted IT meter or a
    facility scope missing a load - and reporting it as an achievement is worse
    than reporting nothing.
    """
    ok, note = p.classify(0.85)
    assert ok is False
    assert "physically impossible" in note


def test_a_very_high_pue_is_reported_with_context_not_refused():
    """It is a real reading during very low IT load, when the largely fixed
    facility overhead dominates."""
    ok, note = p.classify(3.8)
    assert ok is True
    assert "low IT load" in note


def test_no_value_is_not_plausible():
    ok, _ = p.classify(None)
    assert ok is False


# --- what the category means -------------------------------------------------

def test_every_category_names_its_measurement_point():
    """An unlabelled PUE cannot be compared with anyone else's: the same site
    reports a lower number at Category 3 than at Category 1."""
    for cat in (1, 2, 3):
        assert p.CATEGORY_POINT[cat]


def test_it_energy_is_taken_at_the_ups_output():
    """Green Grid Category 1. The UPS Modbus point is Energy_Delivered - energy
    OUT of the UPS - and the UPS feeds only the IT path on this fleet, with
    mechanical plant hanging off the MCC upstream of it."""
    assert p.IT_TYPES_L1 == ["ups"]
    assert "UPS output" in p.CATEGORY_POINT[1]


def test_facility_energy_is_taken_at_the_boundary():
    assert p.FACILITY_TYPES == ["utility_feed"]


# --- the fallback ------------------------------------------------------------

def test_power_may_only_stand_in_for_a_window_that_is_asking_about_now():
    """Present power says nothing about last Tuesday.

    Answering a historical question with today's reading is not a degraded
    answer, it is a different answer wearing the same label.
    """
    assert p.POWER_FALLBACK_WINDOW_S <= 3600
