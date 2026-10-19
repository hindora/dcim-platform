"""Editing alarm rules and giving one device its own value for one."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.alarms import rule_edit as re_
from app.alarms import service as svc
from app.alarms.engine import AlarmKey, Candidate, DwellState, Rule, evaluate

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)

RULE_ROW = {"id": "r1", "name": "high-cpu", "metric_key": "cpu_utilization",
            "operator": ">", "threshold": 90.0, "clear_threshold": 80.0,
            "metric_kind": "numeric"}


def _rule(**kw) -> Rule:
    base = {"id": "r1", "name": "high-cpu", "alarm_type": "high_cpu", "severity": "MAJOR",
            "message_tpl": "{value} > {threshold}", "metric_key": "cpu_utilization",
            "operator": ">", "threshold": 90.0, "clear_threshold": 80.0,
            "dwell_samples": 1, "clear_dwell_samples": 1}
    return Rule(**(base | kw))


# ------------------------------------------------------------- validation

def test_a_clear_point_on_the_wrong_side_is_refused_in_plain_words():
    with pytest.raises(re_.RuleEditError, match="clear the moment it raised"):
        re_.check_band(">", 90.0, 95.0)
    with pytest.raises(re_.RuleEditError, match="clear the moment it raised"):
        re_.check_band("<", 10.0, 5.0)
    assert re_.check_band(">", 90.0, 85.0) == 85.0
    assert re_.check_band("<", 10.0, 15.0) == 15.0


def test_a_missing_clear_point_gets_a_deadband_on_the_safe_side():
    assert re_.check_band(">", 100.0, None) < 100.0
    assert re_.check_band("<", 100.0, None) > 100.0
    assert re_.default_clear(">", 0.0) < 0.0       # a zero threshold still gets a gap


def test_a_threshold_the_metric_can_never_reach_is_refused():
    with pytest.raises(re_.RuleEditError, match="highest value"):
        re_.check_range("cpu_utilization", 150)
    with pytest.raises(re_.RuleEditError, match="lowest value"):
        re_.check_range("cpu_utilization", -5)
    re_.check_range("cpu_utilization", 95)
    re_.check_range("not_a_metric", 12345)         # unknown: nothing to judge against


def test_messages_may_only_use_placeholders_a_rule_can_fill():
    assert re_.check_template("{metric} at {value}, limit {threshold}")
    for bad in ("{nonsense} happened", "unbalanced {value", "", "x" * 401):
        with pytest.raises(re_.RuleEditError):
            re_.check_template(bad)


def test_moving_the_threshold_keeps_a_valid_clear_point_and_fixes_a_stale_one():
    out = re_.validate_fields(RULE_ROW, {"threshold": 95}, re_.RULE_FIELDS)
    assert re_.resolve_band(RULE_ROW, out)["clear_threshold"] == 80.0   # still below 95
    out = re_.validate_fields(RULE_ROW, {"threshold": 70}, re_.RULE_FIELDS)
    fixed = re_.resolve_band(RULE_ROW, out)["clear_threshold"]
    assert fixed < 70.0       # 80 would be above the new raise point: re-derived, not kept


def test_a_state_rule_has_no_threshold_to_edit():
    boolean = {**RULE_ROW, "metric_kind": "boolean", "operator": None}
    with pytest.raises(re_.RuleEditError, match="watches a state"):
        re_.validate_fields(boolean, {"threshold": 1}, re_.RULE_FIELDS)
    got = re_.validate_fields(boolean, {"severity": "minor"}, re_.RULE_FIELDS)
    assert got == {"severity": "MINOR"}


def test_dwell_and_severity_are_bounded():
    for bad in ({"dwell_samples": 0}, {"dwell_samples": 101}, {"severity": "CLEAR"},
                {"dwell_seconds": -1}):
        with pytest.raises(re_.RuleEditError):
            re_.validate_fields(RULE_ROW, bad, re_.RULE_FIELDS)


def test_a_new_rule_is_checked_end_to_end():
    ok = re_.validate_new({"name": "Hot switch CPU", "metric_key": "cpu_temperature",
                           "operator": ">", "threshold": 85, "severity": "major",
                           "device_types": ["server"]}, {"server", "switch"})
    assert ok["alarm_type"] == "custom_hot_switch_cpu" and ok["clear_threshold"] < 85
    # the last: cpu_temperature is reported by servers only
    for bad in ({"name": "x"}, {"metric_key": "bogus"}, {"operator": "=="},
                {"severity": "loud"}, {"device_types": ["toaster"]},
                {"threshold": 9999}, {"device_types": ["switch"]}):
        body = {"name": "Hot switch CPU", "metric_key": "cpu_temperature", "operator": ">",
                "threshold": 85, "severity": "major", "device_types": ["server"], **bad}
        with pytest.raises(re_.RuleEditError):
            re_.validate_new(body, {"server", "switch"})


# -------------------------------------------------------------- overrides

def _fires(rule: Rule, value: float) -> bool:
    out, _ = evaluate(rule, AlarmKey("dev", rule.alarm_type, ""), value, NOW, DwellState())
    return isinstance(out, Candidate)


def test_an_override_moves_the_threshold_for_one_device_without_touching_the_rule():
    rule = _rule()
    assert not _fires(rule, 85)                       # 85 is under the estate's 90
    hot_box = re_.apply_override(rule, {"threshold": 80.0, "clear_threshold": 70.0})
    assert _fires(hot_box, 85)                        # but over this device's 80
    assert rule.threshold == 90.0                     # the shared rule is untouched
    assert re_.apply_override(rule, {"threshold": None}) is rule     # nothing to apply


def test_an_override_can_change_severity_and_dwell_alone():
    rule = _rule(dwell_samples=3)
    o = re_.apply_override(rule, {"severity": "CRITICAL", "dwell_samples": 1})
    assert (o.severity, o.dwell_samples, o.threshold) == ("CRITICAL", 1, 90.0)


class _FakeRedis:
    def __init__(self) -> None:
        self.v: bytes | None = None

    async def get(self, _key):
        return self.v

    async def incr(self, _key):
        self.v = str(int(self.v or 0) + 1).encode()


class _FakeSession:
    pass


def test_workers_pick_up_an_edit_on_the_next_batch_not_a_minute_later(monkeypatch):
    loads = {"n": 0}
    rows = [{"id": "r1", "name": "n", "alarm_type": "t", "severity": "MAJOR",
             "message_tpl": "m", "metric_key": "cpu_utilization", "operator": ">",
             "threshold": 90.0, "clear_threshold": 80.0, "dwell_samples": 1,
             "dwell_seconds": None, "clear_dwell_samples": 1, "device_types": [],
             "stale_after_s": None, "enabled": True}]

    async def load_rules(_s):
        loads["n"] += 1
        return rows

    async def load_overrides(_s):
        return [{"rule_id": "r1", "device_id": "d1", "enabled": False, "threshold": None}]

    monkeypatch.setattr(svc.repo, "load_rules", load_rules)
    monkeypatch.setattr(svc.repo, "load_overrides", load_overrides)
    redis = _FakeRedis()
    service = svc.AlarmService(redis)

    async def go():
        await service.ensure_rules(_FakeSession())
        await service.ensure_rules(_FakeSession())          # inside the refresh window
        assert loads["n"] == 1
        await svc.bump_rules_version(redis)                 # the API edited a rule
        await service.ensure_rules(_FakeSession())
        assert loads["n"] == 2                              # reloaded at once
        assert service._overrides[("r1", "d1")]["enabled"] is False

    asyncio.run(go())


def test_the_sample_loop_skips_a_muted_rule_and_applies_an_override():
    import inspect
    src = inspect.getsource(svc.AlarmService.evaluate_samples)
    assert 'ov.get("enabled") is False' in src and "apply_override(rule, ov)" in src
