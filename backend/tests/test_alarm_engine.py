"""Dwell, hysteresis and the alarm key.

These are the cases that decide whether an alarm list is usable. A rule without
a deadband raises and clears on every sample while a metric sits on its
threshold; at fleet scale that is hundreds of alarms an hour and an operator who
stops reading them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.alarms.engine import (
    AlarmKey,
    Candidate,
    ClearSignal,
    DwellState,
    Rule,
    escalates,
    evaluate,
)

T0 = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
KEY = AlarmKey("dev-1", "cpu_temp_high", "")


def rule(**kw) -> Rule:
    base = {
        "id": "r1", "name": "cpu-temp-high", "alarm_type": "cpu_temp_high",
        "severity": "WARNING",
        "message_tpl": "CPU {value} C above {threshold} C",
        "metric_key": "cpu_temperature", "operator": ">", "threshold": 80.0,
        "clear_threshold": 75.0, "dwell_samples": 3, "clear_dwell_samples": 2,
    }
    base.update(kw)
    return Rule(**base)


def feed(r: Rule, values, start=T0, step=30):
    """Run a series of samples, returning the actions and the final state."""
    state = DwellState()
    actions = []
    for i, v in enumerate(values):
        out, state = evaluate(r, KEY, v, start + timedelta(seconds=i * step), state)
        if out is not None:
            actions.append(out)
    return actions, state


def test_dwell_suppresses_a_single_spike():
    # One sample over the line is not an alarm. Alarming on it is how a
    # transient becomes a page at 3am.
    actions, _ = feed(rule(), [85.0])
    assert actions == []


def test_raises_only_after_the_dwell_is_satisfied():
    actions, _ = feed(rule(), [85.0, 86.0, 87.0])
    assert len(actions) == 1
    assert isinstance(actions[0], Candidate)
    assert actions[0].severity == "WARNING"
    assert actions[0].value == 87.0


def test_hysteresis_holds_inside_the_deadband():
    """Between clear_threshold and threshold nothing happens.

    This branch is the entire point of hysteresis and the one most often
    missing.
    """
    r = rule()
    actions, state = feed(r, [85.0, 86.0, 87.0])
    assert len(actions) == 1

    # 78, 77, 76 are all below the raise threshold but above the clear
    # threshold: no clear may be emitted.
    for v in (78.0, 77.0, 76.0):
        out, state = evaluate(r, KEY, v, T0, state)
        assert out is None, f"{v} inside the deadband should change nothing"


def test_clears_only_after_the_clear_dwell():
    r = rule()
    actions, state = feed(r, [85.0, 86.0, 87.0])
    assert len(actions) == 1

    out, state = evaluate(r, KEY, 74.0, T0, state)
    assert out is None, "one sample below the clear threshold is not yet a clear"

    out, state = evaluate(r, KEY, 73.0, T0, state)
    assert isinstance(out, ClearSignal)
    assert out.key == KEY


def test_flapping_around_the_threshold_never_clears():
    """81/79 oscillation sits inside the 75-80 deadband on the way down.

    The engine keeps producing a candidate while the condition holds - that is
    what refreshes last_seen and the occurrence count - and collapsing repeats
    into one alarm is the alarm key's job in the database, not this function's.
    What must NEVER happen is a clear: an alarm that flaps open and shut on
    every other sample is the failure this deadband exists to prevent.
    """
    r = rule()
    values = [85.0, 86.0, 87.0] + [79.0, 81.0] * 10
    actions, _ = feed(r, values)

    clears = [a for a in actions if isinstance(a, ClearSignal)]
    assert clears == [], "a value inside the deadband must never clear"

    # Candidates only ever come from the breaching samples, never the 79s.
    raises = [a for a in actions if isinstance(a, Candidate)]
    assert all(a.value == 81.0 or a.value >= 85.0 for a in raises)
    assert len(raises) == 1 + 10, "one initial raise plus one per breaching sample"


def test_without_hysteresis_the_engine_still_needs_the_clear_dwell():
    # clear_threshold=None means clear at the raise threshold. The clear dwell
    # is then the only thing damping it, so it must still be honoured.
    r = rule(clear_threshold=None, clear_dwell_samples=2)
    actions, state = feed(r, [85.0, 86.0, 87.0])
    assert len(actions) == 1
    out, state = evaluate(r, KEY, 70.0, T0, state)
    assert out is None
    out, state = evaluate(r, KEY, 70.0, T0, state)
    assert isinstance(out, ClearSignal)


def test_dwell_seconds_is_wall_time_not_sample_count():
    """A rule with dwell_seconds must not fire early just because samples are
    frequent - otherwise a 5-second poll interval turns a 90-second dwell into
    15 seconds."""
    r = rule(dwell_samples=1, dwell_seconds=90)
    state = DwellState()
    out, state = evaluate(r, KEY, 85.0, T0, state)
    assert out is None
    out, state = evaluate(r, KEY, 85.0, T0 + timedelta(seconds=30), state)
    assert out is None
    out, state = evaluate(r, KEY, 85.0, T0 + timedelta(seconds=95), state)
    assert isinstance(out, Candidate)


def test_a_broken_breach_restarts_the_dwell():
    r = rule()
    state = DwellState()
    for v in (85.0, 86.0):
        out, state = evaluate(r, KEY, v, T0, state)
        assert out is None
    # Drops well clear, resetting progress.
    out, state = evaluate(r, KEY, 60.0, T0, state)
    assert out is None
    # Two more breaches must NOT be enough - the counter restarted.
    for v in (85.0, 86.0):
        out, state = evaluate(r, KEY, v, T0, state)
        assert out is None
    out, state = evaluate(r, KEY, 87.0, T0, state)
    assert isinstance(out, Candidate)


def test_less_than_rules_work_in_the_same_shape():
    # Low humidity: raise below 20, clear above 25.
    r = rule(alarm_type="humidity_low", metric_key="relative_humidity",
             operator="<", threshold=20.0, clear_threshold=25.0,
             dwell_samples=2, clear_dwell_samples=1)
    actions, state = feed(r, [15.0, 14.0])
    assert len(actions) == 1 and isinstance(actions[0], Candidate)

    out, state = evaluate(r, KEY, 22.0, T0, state)
    assert out is None, "22 is inside the 20-25 deadband"

    out, state = evaluate(r, KEY, 30.0, T0, state)
    assert isinstance(out, ClearSignal)


def test_message_template_failure_still_raises():
    # A bad template must never be the reason an alarm goes unreported.
    r = rule(message_tpl="{nonexistent_field} broke")
    actions, _ = feed(r, [85.0, 86.0, 87.0])
    assert len(actions) == 1
    assert "cpu_temp_high" in actions[0].message


def test_rule_scoping_by_device_type():
    r = rule(device_types=("server",))
    assert r.applies_to("server")
    assert not r.applies_to("switch")
    assert rule().applies_to("anything"), "an empty scope means all device types"


def test_dwell_state_survives_a_round_trip():
    # The state lives in Redis between batches; a lossy encoding would silently
    # restart every dwell.
    st = DwellState(breach_count=2, clear_count=0, first_breach_us=1_755_512_400_000_000)
    assert DwellState.from_wire(st.to_wire()) == st
    assert DwellState.from_wire(None) == DwellState()
    assert DwellState.from_wire("garbage") == DwellState()


@pytest.mark.parametrize(("prev", "cur", "expected"), [
    ("WARNING", "CRITICAL", True),
    ("CRITICAL", "WARNING", False),
    ("MAJOR", "MAJOR", False),
    ("CLEAR", "INFO", True),
])
def test_escalation_ordering(prev, cur, expected):
    assert escalates(prev, cur) is expected
