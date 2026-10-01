"""The rollout policy (docs/26 Phase 7), pure: one member per pool at a time,
never a pool's last healthy member, settle before the next, halt on failure."""

from __future__ import annotations

from app.services.rollout import SETTLE_S, Member, decide

V = "1.2.0"


def m(cid, pool="p1", version="1.1.0", hb=5.0, healthy_s=3600.0, state="active"):
    return Member(collector_id=cid, pool_id=pool, version=version, heartbeat_age_s=hb,
                  healthy_duration_s=healthy_s, state=state)


def cmd(cid, state="succeeded", detail=""):
    return {"collector_id": cid, "state": state, "result": {"detail": detail}}


def test_one_member_per_pool_first():
    d = decide(V, ["p1"], [m("a"), m("b")], [])
    assert d.upgrade == ["a"] and not d.done


def test_pools_advance_in_parallel_one_member_each():
    d = decide(V, ["p1", "p2"], [m("a"), m("b"), m("c", "p2"), m("d", "p2")], [])
    assert d.upgrade == ["a", "c"]


def test_nothing_new_while_a_member_is_upgrading():
    d = decide(V, ["p1"], [m("a"), m("b")], [cmd("a", "delivered")])
    assert d.upgrade == [] and "upgrading" in d.waiting["p1"]


def test_the_next_member_waits_for_the_last_to_settle():
    just_back = m("a", version=V, healthy_s=SETTLE_S - 10)
    d = decide(V, ["p1"], [just_back, m("b")], [cmd("a")])
    assert d.upgrade == [] and "settling" in d.waiting["p1"]
    settled = m("a", version=V, healthy_s=SETTLE_S + 10)
    assert decide(V, ["p1"], [settled, m("b")], [cmd("a")]).upgrade == ["b"]


def test_a_member_that_reported_success_but_runs_the_old_version_holds_the_pool():
    d = decide(V, ["p1"], [m("a", version="1.1.0"), m("b")], [cmd("a")])
    assert d.upgrade == [] and "settling" in d.waiting["p1"]


def test_never_take_the_last_healthy_member_down():
    d = decide(V, ["p1"], [m("a"), m("b", hb=400.0)], [])
    assert d.upgrade == [] and "unhealthy" in d.waiting["p1"]


def test_a_single_member_pool_is_upgraded_anyway():
    """No partner to cover it: a short outage, allowed rather than never."""
    assert decide(V, ["p1"], [m("a")], []).upgrade == ["a"]


def test_a_failed_or_expired_upgrade_halts_the_whole_rollout():
    d = decide(V, ["p1", "p2"], [m("a"), m("c", "p2")],
               [cmd("a", "failed", "rolled back: no healthy heartbeat")])
    assert d.failed and "rolled back" in d.failed and d.upgrade == []
    assert decide(V, ["p1"], [m("a")], [cmd("a", "expired")]).failed


def test_done_when_every_member_runs_the_target():
    d = decide(V, ["p1"], [m("a", version=V), m("b", version=V)], [cmd("a"), cmd("b")])
    assert d.done and d.upgrade == []


def test_draining_and_retired_members_are_not_touched():
    d = decide(V, ["p1"], [m("a", state="draining"), m("b")], [])
    assert d.upgrade == ["b"]
