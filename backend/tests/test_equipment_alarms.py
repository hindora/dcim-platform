"""Boolean rules: the plant's own fault points, finally evaluated.

A binary point is not a threshold with two values. It has no deadband - there
is nothing between asserted and not asserted for hysteresis to sit in - and its
message names the point rather than reporting that something equals 1.0. Both
of those are easy to get wrong by reusing the numeric path, so they are pinned
here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.alarms.engine import (
    AlarmKey,
    Candidate,
    ClearSignal,
    DwellState,
    Rule,
    evaluate,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _rule(**over) -> Rule:
    base = {
        "id": "r-equip", "name": "equipment-alarm-major",
        "alarm_type": "equipment_alarm", "severity": "MAJOR",
        "message_tpl": "{point} reported by the equipment",
        "metric_key": "alarm_state", "metric_kind": "boolean",
        "raise_on": True, "dwell_samples": 2, "clear_dwell_samples": 2,
        "instances": ("Alarm_Leak", "Battery_Fault"),
    }
    base.update(over)
    return Rule(**base)


def _feed(rule: Rule, key: AlarmKey, values, start=NOW):
    """Run a series of samples through one rule, returning (actions, state)."""
    state = DwellState()
    actions = []
    for i, v in enumerate(values):
        action, state = evaluate(rule, key, 1.0 if v else 0.0,
                                 start + timedelta(seconds=30 * i), state)
        actions.append(action)
    return actions, state


def test_a_point_must_hold_before_it_raises():
    """One flap of a contact is not a fault."""
    key = AlarmKey("dev-1", "equipment_alarm", "Alarm_Leak")
    actions, _ = _feed(_rule(), key, [True])
    assert actions == [None]

    actions, _ = _feed(_rule(), key, [True, True])
    assert isinstance(actions[-1], Candidate)


def test_it_clears_when_the_point_de_asserts():
    key = AlarmKey("dev-1", "equipment_alarm", "Alarm_Leak")
    actions, _ = _feed(_rule(), key, [True, True, False, False])
    assert isinstance(actions[1], Candidate)
    assert isinstance(actions[-1], ClearSignal)


def test_a_single_de_assert_does_not_clear():
    """Clear dwell applies both ways: a point that drops once may be chattering."""
    key = AlarmKey("dev-1", "equipment_alarm", "Battery_Fault")
    actions, _ = _feed(_rule(), key, [True, True, False])
    assert actions[-1] is None


def test_the_message_names_the_point_not_a_number():
    """The instance IS the fault; reporting `1.0` would say nothing."""
    key = AlarmKey("dev-1", "equipment_alarm", "Alarm_Leak")
    actions, _ = _feed(_rule(), key, [True, True])
    candidate = actions[-1]
    assert isinstance(candidate, Candidate)
    assert "Alarm Leak" in candidate.message
    assert "1.0" not in candidate.message
    # Detection is state-reported, not a threshold crossing.
    assert candidate.source == "state"
    assert candidate.threshold is None


def test_run_status_points_can_raise_on_false():
    """A run-status point is the inverse: not running is the fault.

    Nothing seeds such a rule yet - a staged-off chiller is not running BY
    DESIGN and would alarm the standby half of the plant - but the engine has
    to support it before lead/lag awareness can use it.
    """
    rule = _rule(raise_on=False, metric_key="equipment_state",
                 instances=("Unit_Running",))
    key = AlarmKey("dev-1", "equipment_alarm", "Unit_Running")
    actions, _ = _feed(rule, key, [False, False])
    assert isinstance(actions[-1], Candidate)

    # A healthy point produces no Candidate. It does emit a ClearSignal once
    # the clear dwell is met, with nothing to clear - which is normal and the
    # numeric path does the same: `clear_alarms` treats a clear with no
    # matching raise as a no-op, and that is what makes recovery idempotent
    # after a restart.
    actions, _ = _feed(rule, key, [True, True])
    assert not any(isinstance(a, Candidate) for a in actions)


def test_severity_is_assigned_per_point():
    """Points share one metric, so the instance list is what separates a leak
    from a dirty filter."""
    major = _rule(instances=("Alarm_Leak",), severity="MAJOR")
    warning = _rule(name="equipment-alarm-warning", severity="WARNING",
                    instances=("Filter_Dirty",))

    assert major.applies_to_instance("Alarm_Leak")
    assert not major.applies_to_instance("Filter_Dirty")
    assert warning.applies_to_instance("Filter_Dirty")
    assert not warning.applies_to_instance("Alarm_Leak")


def test_an_empty_instance_list_means_every_instance():
    rule = _rule(instances=())
    assert rule.applies_to_instance("anything")
    assert rule.applies_to_instance("")


def test_numeric_rules_are_untouched():
    """The boolean branch must not change threshold behaviour."""
    rule = Rule(id="r1", name="cpu-high", alarm_type="cpu_high",
                severity="WARNING", message_tpl="CPU {value}%",
                metric_key="cpu_utilization", operator=">", threshold=90.0,
                clear_threshold=80.0, dwell_samples=2, clear_dwell_samples=2)
    key = AlarmKey("dev-1", "cpu_high", "")

    state = DwellState()
    for value in (95.0, 95.0):
        action, state = evaluate(rule, key, value, NOW, state)
    assert isinstance(action, Candidate)

    # 85 is inside the deadband: neither breaching nor recovered.
    action, state = evaluate(rule, key, 85.0, NOW, state)
    assert action is None

    for value in (70.0, 70.0):
        action, state = evaluate(rule, key, value, NOW, state)
    assert isinstance(action, ClearSignal)


def test_every_arriving_point_has_a_severity():
    """The seeded rules must cover every point the plane emits.

    Severity is assigned per instance, so a point in neither list is a point
    that streams in and raises nothing - which is the exact condition phase 2
    exists to end. Checked against the same inventory the taxonomy tests use.
    """
    import importlib

    from tests.test_alert_taxonomy import SIMULATOR_ALARM_POINTS

    seed = importlib.import_module("app.core.equipment_points")

    covered = set(seed.MAJOR_POINTS) | set(seed.WARNING_POINTS)
    arriving = {point for _role, point in SIMULATOR_ALARM_POINTS}

    assert not (arriving - covered), (
        f"points with no severity assigned: {sorted(arriving - covered)}")
    assert not (covered - arriving), (
        f"severity assigned to points nothing emits: {sorted(covered - arriving)}")
    assert not (set(seed.MAJOR_POINTS) & set(seed.WARNING_POINTS)), (
        "a point cannot be both MAJOR and WARNING")
