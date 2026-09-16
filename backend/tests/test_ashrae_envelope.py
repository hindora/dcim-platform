"""The ASHRAE envelope, which is three limits rather than one.

These are the cases the old single-leg check got wrong, written down so the
next edit cannot quietly drop one.
"""
import pytest

from app.core.ashrae import (
    ALLOWABLE,
    IN_BAND,
    OUT,
    dew_point_c,
    envelope_for,
    grade_envelope,
    grade_moisture,
    grade_rate,
    grade_temperature,
)


def test_an_unclassified_room_is_graded_as_a1():
    """The tightest envelope, because an estate that has not said otherwise
    should be held to the strictest thing its kit might be."""
    assert envelope_for(None).name == "A1"
    assert envelope_for("").name == "A1"
    assert envelope_for("a3").name == "A3"       # case is not a classification
    assert envelope_for("nonsense").name == "A1"


def test_class_moves_the_allowable_ceiling_and_not_the_recommended_band():
    """That is what 'recommended' means: the band you run in, the same for
    every class. Only what the hardware survives changes."""
    a1, a3 = envelope_for("A1"), envelope_for("A3")
    assert (a1.rec_low_c, a1.rec_high_c) == (a3.rec_low_c, a3.rec_high_c)
    assert a1.allow_high_c == 32.0 and a3.allow_high_c == 40.0
    # 33 C is equipment at risk in an A1 hall and equipment inside its own
    # specification in an A3 one.
    assert grade_temperature(33.0, a1) == OUT
    assert grade_temperature(33.0, a3) == ALLOWABLE


def test_both_moisture_ceilings_bind_and_the_worse_one_wins():
    """60 % RH and a 15 C dew point are two limits, not one said twice, and
    which bites depends on the temperature. A page checking only RH passes a
    hall ASHRAE fails."""
    a1 = envelope_for("A1")
    # 27 C at 60 % RH is a dew point of 18.6 - past the recommended 15 AND past
    # the allowable 17, while the humidity leg alone would have said in band.
    assert dew_point_c(27.0, 60.0) == pytest.approx(18.6, abs=0.1)
    assert grade_moisture(dew_point_c(27.0, 60.0), 60.0, a1) == OUT
    assert grade_moisture(None, 60.0, a1) == IN_BAND       # RH leg alone: fine
    # The same 60 % at 20 C is 12.0 C dew point, comfortably inside.
    assert grade_moisture(dew_point_c(20.0, 60.0), 60.0, a1) == IN_BAND


def test_dry_air_fails_on_the_floor_not_the_ceiling():
    """The limit a dry hall breaches is the dew-point FLOOR, and it is the one
    a humidity ceiling can never see. 18 C at 8 % RH is -16.8 C dew point:
    outside even the allowable envelope, on a floor building static charge."""
    a1 = envelope_for("A1")
    assert dew_point_c(18.0, 8.0) == pytest.approx(-16.8, abs=0.1)
    assert grade_moisture(dew_point_c(18.0, 8.0), 8.0, a1) == OUT
    assert grade_temperature(18.0, a1) == IN_BAND          # the dry bulb is fine


def test_a_rack_with_no_moisture_probe_is_unmeasured_not_compliant():
    """Most racks carry a bare thermistor. That is missing evidence, not a
    pass, and it must not quietly grade the hall."""
    a1 = envelope_for("A1")
    assert grade_moisture(None, None, a1) is None
    assert grade_rate(None, a1) is None
    # An unmeasured leg does not fail the reading either.
    assert grade_envelope(22.0, a1) == IN_BAND


def test_rate_is_judged_on_speed_not_direction():
    """A 25 K/hour collapse after a chiller trip and a 25 K/hour recovery are
    the same stress on the same hardware. ASHRAE writes one number, so there is
    no allowable tier - a breach is straight out."""
    a1 = envelope_for("A1")
    assert grade_rate(19.9, a1) == IN_BAND
    assert grade_rate(25.0, a1) == OUT
    assert grade_rate(-25.0, a1) == OUT
    # A tape room is held to 5, set per room because no telemetry can tell you
    # what medium is in a rack.
    tape = envelope_for("A1", max_rate_k_per_h=5.0)
    assert grade_rate(8.0, a1) == IN_BAND
    assert grade_rate(8.0, tape) == OUT


def test_the_envelope_is_the_worst_leg_that_could_be_judged():
    """One number to quote, three to diagnose. A hall holding 22 C all day is
    not compliant if it is doing it at 70 % RH, or if it got there at
    30 K/hour."""
    a1 = envelope_for("A1")
    assert grade_envelope(22.0, a1, dp_c=dew_point_c(22.0, 50.0),
                          rh_pct=50.0, rate_k_per_h=1.0) == IN_BAND
    assert grade_envelope(22.0, a1, dp_c=dew_point_c(22.0, 70.0),
                          rh_pct=70.0, rate_k_per_h=1.0) == ALLOWABLE
    assert grade_envelope(22.0, a1, dp_c=dew_point_c(22.0, 50.0),
                          rh_pct=50.0, rate_k_per_h=30.0) == OUT
    # And the temperature leg still decides when it is the worst one.
    assert grade_envelope(34.0, a1, dp_c=dew_point_c(34.0, 30.0),
                          rh_pct=30.0, rate_k_per_h=1.0) == OUT
