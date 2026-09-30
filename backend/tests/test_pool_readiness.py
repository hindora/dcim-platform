"""summarise_readiness - the onboarding wizard's verdict on a pool (docs/26
Phase 8), pure: no database, just the rows the live query would return."""

from __future__ import annotations

from app.services.pools import CREDENTIALED, summarise_readiness


def _pool(**over):
    return {"id": "p1", "min_members": 1, "accepting_members": 1, "unassigned": 0,
            "members": [{"collector_id": "c1", "state": "active",
                         "healthy": True, "accepting": True}], **over}


def _proto(protocol, endpoints, with_credential=0, online=None):
    online = endpoints if online is None else online
    return {"protocol": protocol, "endpoints": endpoints,
            "with_credential": with_credential, "online": online,
            "degraded": 0, "offline": endpoints - online, "never_polled": 0}


def _checks(r):
    return {c["key"]: c["ok"] for c in r["checks"]}


def test_a_fully_collecting_pool_is_ready():
    r = summarise_readiness(_pool(), [_proto("snmp", 3, 3), _proto("bacnet", 2)],
                            {"c1": {"passed": True, "ran_at": "t"}})
    assert r["ready"] is True
    assert all(ok is True for ok in _checks(r).values())


def test_bacnet_and_modbus_never_count_as_missing_a_credential():
    assert "bacnet" not in CREDENTIALED and "modbus" not in CREDENTIALED
    r = summarise_readiness(_pool(), [_proto("bacnet", 5), _proto("modbus", 4)],
                            {"c1": {"passed": True, "ran_at": "t"}})
    assert _checks(r)["credentials"] is True
    assert r["ready"] is True


def test_a_credentialed_protocol_without_one_is_flagged():
    r = summarise_readiness(_pool(), [_proto("redfish", 4, 1)],
                            {"c1": {"passed": True, "ran_at": "t"}})
    assert r["protocols"][0]["missing_credential"] == 3
    assert _checks(r)["credentials"] is False
    assert r["ready"] is False


def test_an_empty_pool_is_undecided_not_green():
    """No endpoints is not "all online" - the last step must not go green
    on a site nothing has been discovered at."""
    r = summarise_readiness(_pool(), [], {"c1": {"passed": True, "ran_at": "t"}})
    c = _checks(r)
    assert c["endpoints"] is None and c["online"] is None and c["credentials"] is None
    assert r["ready"] is False


def test_a_member_that_never_ran_preflight_is_not_a_pass():
    r = summarise_readiness(_pool(), [_proto("snmp", 1, 1)], {"c1": None})
    assert r["members"][0]["preflight_passed"] is None
    assert _checks(r)["preflight"] is False


def test_no_members_fails_members_and_leaves_preflight_undecided():
    r = summarise_readiness(_pool(members=[], accepting_members=0),
                            [_proto("snmp", 1, 1)], {})
    c = _checks(r)
    assert c["members"] is False
    assert c["preflight"] is None


def test_min_members_above_one_needs_that_many_accepting():
    r = summarise_readiness(_pool(min_members=2), [_proto("snmp", 1, 1)],
                            {"c1": {"passed": True, "ran_at": "t"}})
    assert _checks(r)["members"] is False


def test_unassigned_endpoints_fail_the_assigned_check():
    r = summarise_readiness(_pool(unassigned=2), [_proto("snmp", 2, 2)],
                            {"c1": {"passed": True, "ran_at": "t"}})
    assert _checks(r)["assigned"] is False


def test_a_record_that_never_ran_is_not_healthy():
    """A collector created ahead of its install carries a creation-time
    heartbeat; the wizard must not call it healthy before it ever ran."""
    pool = _pool(accepting_members=0, members=[{
        "collector_id": "c1", "state": "active", "healthy": True,
        "accepting": False, "has_run": False}])
    r = summarise_readiness(pool, [], {"c1": None})
    assert r["members"][0]["healthy"] is False
    assert _checks(r)["members"] is False


def test_undecided_checks_do_not_read_as_passes():
    r = summarise_readiness(_pool(), [], {"c1": {"passed": True, "ran_at": "t"}})
    for c in r["checks"]:
        if c["ok"] is None and c["key"] != "endpoints":
            assert c["detail"] == "nothing to check yet", c
