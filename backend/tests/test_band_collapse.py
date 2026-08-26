"""A warning and a critical on one measurement are one condition.

Three faults injected on SRV03-DC2-HA-R2-01 produced five alarms:

    cpu_high          MAJOR      trap
    cpu_saturated     CRITICAL   trap        <- same CPU
    cpu_temp_critical CRITICAL   threshold
    cpu_temp_high     WARNING    threshold   <- same sensor
    memory_high       MAJOR      trap

Two rules on one measurement fired together, because 93 C crosses `>80` and
`>90` at the same time. The console showed both: two rows, two severities, two
acknowledgements and two clears for one hot CPU.

Worse, the WARNING arrived a minute AFTER its CRITICAL - the two rules carry
different dwells, 3 samples against 2 - so an operator reading the list
top-down saw the situation apparently improving while nothing had changed.

ISA-18.2 asks for one alarm per measurement point with a severity that
escalates. The rules stay separate here: they hold different thresholds,
dwells and response classes, and both are genuinely true. The lower one is
folded under the higher with the same machinery a dependency symptom uses -
the record keeps both, the console shows the one that matters.
"""

from __future__ import annotations

import inspect

import pytest

from app.alarms import correlation, service


def _sql(name: str) -> str:
    return str(getattr(correlation, name))


# ------------------------------------------------------------- which is higher


def test_a_band_is_ranked_by_threshold_not_by_severity():
    """Severity is a label somebody chose; the threshold is the measurement.

    Two rules can share a severity - and a critical band with a WARNING label
    would then fold the wrong way round.
    """
    sql = _sql("_BAND_ROOT")
    assert "r.threshold > me.threshold" in sql
    assert "r.threshold < me.threshold" in sql

    # The clause that DECIDES which band is higher must not consult severity.
    # It is read out of the CTE rather than the whole statement, because the
    # outer SELECT legitimately returns the severity for the caller to log.
    higher = sql[sql.index("higher AS ("):sql.index("SELECT a.id")]
    assert "severity" not in higher


def test_direction_follows_the_operator():
    """`<` rules invert: a LOWER threshold is the worse band.

    A low-humidity or low-voltage rule alarms as the value falls, so ranking by
    "bigger is worse" would fold the critical under the warning.
    """
    for sql_name in ("_BAND_ROOT", "_LOWER_BANDS"):
        sql = _sql(sql_name)
        assert "me.operator = '>'" in sql
        assert "me.operator = '<'" in sql


def test_only_the_same_measurement_is_a_band():
    """Two conditions on one device are not bands unless they measure one thing.

    Without the metric_key test, a hot CPU would fold a memory alarm under it -
    both are on the device, neither explains the other.
    """
    for sql_name in ("_BAND_ROOT", "_LOWER_BANDS"):
        assert "r.metric_key = me.metric_key" in _sql(sql_name)


def test_the_instance_is_part_of_the_key():
    """Bands are per measurement POINT.

    Two sensors on one chassis are two measurements: folding sensor 2's
    warning under sensor 1's critical hides a second hot spot.
    """
    for sql_name in ("_BAND_ROOT", "_LOWER_BANDS"):
        assert "a.instance IS NOT DISTINCT FROM :instance" in _sql(sql_name)


def test_a_band_root_is_never_itself_suppressed():
    """Folding under an already-folded alarm would hide both.

    The higher band has to be visible for the collapse to leave anything on
    screen.
    """
    assert "NOT a.is_symptom" in _sql("_BAND_ROOT")


def test_cleared_alarms_are_not_band_roots():
    for sql_name in ("_BAND_ROOT", "_LOWER_BANDS"):
        assert "a.state <> 'CLEARED'" in _sql(sql_name)


# ----------------------------------------------------------- both directions


def test_the_collapse_works_whichever_band_arrives_first():
    """Either order happens, and neither is unusual.

    A value that climbs raises the warning first; a value that jumps past both
    raises the critical first. The differing dwells mean arrival order does not
    even follow the reading.
    """
    src = inspect.getsource(correlation.collapse_bands)
    assert "_BAND_ROOT" in src, "no check for an existing higher band"
    assert "_LOWER_BANDS" in src, "open lower bands are not folded under a new higher one"


# --------------------------------------------------------------- both paths


@pytest.mark.parametrize("fn", ["handle_event", "_apply_candidate"])
def test_every_raise_path_collapses(fn):
    """A trap fires at the vendor's threshold, a rule at ours.

    Collapsing on only one path leaves the console showing one row from each,
    which is the shape of the original bug.
    """
    src = inspect.getsource(getattr(service.AlarmService, fn))
    assert "_collapse_bands" in src, f"{fn} does not collapse bands"


def test_a_dependency_symptom_is_not_folded_twice():
    """An alarm already explained by an upstream root keeps that explanation.

    Re-pointing it at a band sibling would lose the more useful answer: the
    switch is down, not the CPU is warm.
    """
    src = inspect.getsource(service.AlarmService._apply_candidate)
    collapse = src[src.index("_collapse_bands") - 400:src.index("_collapse_bands")]
    assert 'is_symptom' in collapse, "the band collapse ignores an existing root"


def test_the_release_path_is_the_shared_one():
    """When the higher band clears, the lower must come back on its own.

    It uses the same release_symptoms the dependency roots use, so a fix there
    fixes both - and a regression there breaks both loudly rather than one
    quietly.
    """
    src = inspect.getsource(service)
    assert "release_symptoms" in src
