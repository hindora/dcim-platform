"""A lower band cannot honestly need more evidence than the higher one.

`cpu_temp_high` (>80) needed 3 samples of dwell while `cpu_temp_critical`
(>90) needed 2, so a CPU going straight past both raised the CRITICAL a full
poll before its own WARNING - and the alarm list read as though the situation
were improving while nothing had changed.

It is not a tuning preference. A lower band is a SUPERSET of a higher one: a
value that has been above 90 for three samples has been above 80 for at least
three. Demanding MORE evidence for the weaker claim cannot be satisfied earlier
than the stronger one, so the ordering is guaranteed backwards whenever one
reading crosses both.

The reverse is fine and stays: a higher band may react FASTER than the one
below it, which is most of why a second threshold exists.
"""

from __future__ import annotations

from app.alarms.engine import Rule, band_dwell_violations


def _rule(alarm_type, metric, operator, threshold, dwell, enabled=True):
    return Rule(
        id=alarm_type, name=alarm_type, alarm_type=alarm_type,
        severity="WARNING", message_tpl="{value}", metric_key=metric,
        operator=operator, threshold=threshold, clear_threshold=None,
        dwell_samples=dwell, dwell_seconds=None, clear_dwell_samples=2,
        device_types=(), stale_after_s=None, enabled=enabled,
    )


def test_the_case_that_was_live():
    bad = band_dwell_violations([
        _rule("cpu_temp_high", "cpu_temperature", ">", 80, 3),
        _rule("cpu_temp_critical", "cpu_temperature", ">", 90, 2),
    ])
    assert bad == [("cpu_temp_high", "cpu_temp_critical")]


def test_equal_dwells_are_fine():
    assert band_dwell_violations([
        _rule("cpu_high", "cpu_utilization", ">", 80, 3),
        _rule("cpu_saturated", "cpu_utilization", ">", 95, 3),
    ]) == []


def test_a_faster_critical_is_the_whole_point():
    """The higher band reacting sooner is legitimate and must not be flagged."""
    assert band_dwell_violations([
        _rule("temp_high", "cpu_temperature", ">", 80, 2),
        _rule("temp_critical", "cpu_temperature", ">", 90, 5),
    ]) == []


def test_a_falling_metric_inverts_which_band_is_higher():
    """`<` rules alarm as the value drops, so LOWER threshold is the worse band.

    Ranked the other way, a voltage-critical would be treated as the weaker
    claim and the check would pass exactly when it should fire.
    """
    bad = band_dwell_violations([
        _rule("voltage_low", "voltage_ln", "<", 200, 4),
        _rule("voltage_critical", "voltage_ln", "<", 180, 2),
    ])
    assert bad == [("voltage_low", "voltage_critical")]


def test_two_ends_of_a_range_are_not_bands():
    """humidity_low (<20) and humidity_high (>70) are not supersets of anything.

    Treating them as a family would force one end's dwell onto the other for no
    reason at all.
    """
    assert band_dwell_violations([
        _rule("humidity_low", "relative_humidity", "<", 20, 5),
        _rule("humidity_high", "relative_humidity", ">", 70, 5),
    ]) == []


def test_different_metrics_are_never_a_family():
    assert band_dwell_violations([
        _rule("cpu_temp_high", "cpu_temperature", ">", 80, 5),
        _rule("inlet_temp_critical", "inlet_temperature", ">", 32, 2),
    ]) == []


def test_a_disabled_rule_constrains_nothing():
    """Turning a band off must not leave the other one flagged against a ghost."""
    assert band_dwell_violations([
        _rule("cpu_temp_high", "cpu_temperature", ">", 80, 3),
        _rule("cpu_temp_critical", "cpu_temperature", ">", 90, 2, enabled=False),
    ]) == []


def test_a_rule_without_a_threshold_is_not_a_band():
    """State rules - endpoint_unreachable, equipment_alarm - have no threshold."""
    assert band_dwell_violations([
        _rule("cpu_temp_high", "cpu_temperature", ">", 80, 3),
        _rule("equipment_alarm", "cpu_temperature", ">", None, 1),
    ]) == []


def test_three_bands_flag_every_broken_pair():
    """The middle band answers to the top one as well as the bottom to it."""
    bad = band_dwell_violations([
        _rule("warn", "m", ">", 70, 4),
        _rule("high", "m", ">", 80, 3),
        _rule("crit", "m", ">", 90, 1),
    ])
    assert set(bad) == {("warn", "high"), ("warn", "crit"), ("high", "crit")}
