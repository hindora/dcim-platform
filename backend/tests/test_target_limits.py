"""Per-address poll limits and pool budget shares (docs/26 Phases 5 and 9)."""

from __future__ import annotations

import pytest

from app.services import target_limits as tl


def test_a_limit_keeps_only_what_is_set():
    assert tl.validate_limit({"max_concurrent": 1, "min_interval_ms": 100}) == {
        "max_concurrent": 1, "min_interval_ms": 100}
    assert tl.validate_limit({"min_interval_ms": 0}) is None, "a zero gap is no limit"
    assert tl.validate_limit({}) is None
    assert tl.validate_limit(None) is None


@pytest.mark.parametrize("bad", [
    {"max_concurrent": 0}, {"max_concurrent": 65}, {"max_concurrent": "2"},
    {"max_concurrent": True}, {"min_interval_ms": -1}, {"min_interval_ms": 60_001},
    {"min_interval_ms": 1.5}, {"rate": 3}, "fast",
])
def test_a_limit_that_cannot_be_applied_is_refused_with_a_reason(bad):
    with pytest.raises(tl.TargetLimitError):
        tl.validate_limit(bad)


def test_pool_defaults_are_per_polled_protocol():
    assert tl.validate_limits({"modbus": {"max_concurrent": 1}, "snmp": {}}) == {
        "modbus": {"max_concurrent": 1}}
    with pytest.raises(tl.TargetLimitError, match="not a polled protocol"):
        tl.validate_limits({"gnmi": {"max_concurrent": 1}})
    assert tl.validate_limits(None) == {}


def test_the_endpoint_override_beats_the_pool_default_for_its_protocol():
    pool = {"modbus": {"max_concurrent": 1, "min_interval_ms": 200},
            "snmp": {"max_concurrent": 2}}
    assert tl.resolve(None, pool, "modbus") == {"max_concurrent": 1, "min_interval_ms": 200}
    assert tl.resolve({"min_interval_ms": 1000}, pool, "modbus") == {"min_interval_ms": 1000}
    assert tl.resolve(None, pool, "bacnet") is None
    assert tl.resolve(None, None, "snmp") is None
    assert tl.resolve("garbage", "garbage", "snmp") is None, "never refuse an assignment"


def test_each_collector_gets_its_share_of_a_budget_by_what_it_owns():
    owners = {"e1": "a", "e2": "a", "e3": "a", "e4": "b", "e5": None}
    pool_of = {"e1": "p", "e2": "p", "e3": "p", "e4": "p", "e5": "p"}
    budgets = {"p": 400}
    # e5 is owned by nobody, so polled by nobody, so uses none of the budget.
    assert tl.budget_shares(budgets, owners, pool_of, "a") == {"p": 300.0}
    assert tl.budget_shares(budgets, owners, pool_of, "b") == {"p": 100.0}


def test_a_failover_survivor_gets_the_whole_budget():
    owners = {"e1": "b", "e2": "b"}
    pool_of = {"e1": "p", "e2": "p"}
    assert tl.budget_shares({"p": 250}, owners, pool_of, "b") == {"p": 250.0}
    assert tl.budget_shares({"p": 250}, owners, pool_of, "a") == {}, "owns nothing"


def test_an_unbudgeted_pool_has_no_share():
    owners = {"e1": "a"}
    assert tl.budget_shares({"p": None}, owners, {"e1": "p"}, "a") == {}
    assert tl.budget_shares({}, owners, {"e1": "p"}, "a") == {}
