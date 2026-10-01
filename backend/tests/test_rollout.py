"""The rollout policy (docs/26 Phase 7), pure: one member per pool at a time,
drained to its partner before it is touched, never a pool's last healthy
member, settle before the next, halt on failure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.rollout import HANDOFF_S, SETTLE_S, Member, decide

V = "1.2.0"
RID = "r1"
TAG = f"rollout:{RID}"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def m(cid, pool="p1", version="1.1.0", hb=5.0, healthy_s=3600.0, state="active",
      by=None, state_age=None, owned=0):
    return Member(collector_id=cid, pool_id=pool, version=version, heartbeat_age_s=hb,
                  healthy_duration_s=healthy_s, state=state, state_by=by,
                  state_age_s=state_age, owned=owned)


def ours(cid, state_age=HANDOFF_S + 1, owned=0, **kw):
    """A member this rollout drained itself."""
    return m(cid, state="draining", by=TAG, state_age=state_age, owned=owned, **kw)


def cmd(cid, state="succeeded", detail="", finished_ago=SETTLE_S + 10):
    return {"collector_id": cid, "state": state, "result": {"detail": detail},
            "finished_at": NOW - timedelta(seconds=finished_ago) if state == "succeeded" else None}


def run(members, commands=(), pools=("p1",)):
    return decide(V, list(pools), members, list(commands), rollout_id=RID, now=NOW)


def test_a_member_with_a_partner_is_drained_first_not_upgraded():
    d = run([m("a"), m("b")])
    assert d.drain == ["a"] and d.upgrade == [] and not d.done
    assert "handing a" in d.waiting["p1"]


def test_pools_advance_in_parallel_one_member_each():
    d = run([m("a"), m("b"), m("c", "p2"), m("d", "p2")], pools=("p1", "p2"))
    assert d.drain == ["a", "c"]


def test_the_upgrade_waits_until_the_drained_member_owns_nothing():
    d = run([ours("a", owned=12), m("b")])
    assert d.upgrade == [] and "handing a" in d.waiting["p1"]


def test_owning_nothing_still_waits_out_the_hand_off_window():
    """The record moving is not the partner polling: it needs a fetch, then a
    first poll."""
    assert run([ours("a", state_age=HANDOFF_S - 5), m("b")]).upgrade == []
    assert run([ours("a"), m("b")]).upgrade == ["a"]


def test_a_partner_going_sick_mid_drain_returns_the_member_un_upgraded():
    d = run([ours("a"), m("b", hb=400.0)])
    assert d.restore == ["a"] and d.upgrade == []


def test_nothing_new_while_a_member_is_upgrading():
    d = run([ours("a"), m("b")], [cmd("a", "delivered")])
    assert d.upgrade == [] and d.drain == [] and "upgrading" in d.waiting["p1"]


def test_a_confirmed_member_is_returned_to_service():
    d = run([ours("a", version=V), m("b")], [cmd("a")])
    assert d.restore == ["a"] and d.drain == [] and not d.done


def test_a_member_that_reported_success_but_runs_the_old_version_stays_drained():
    d = run([ours("a", version="1.1.0"), m("b")], [cmd("a")])
    assert d.restore == [] and "settling" in d.waiting["p1"]


def test_the_next_member_waits_for_the_last_to_settle_after_it_confirmed():
    """Measured from the command's finish: a re-exec never restarts the
    healthy streak, so healthy_since cannot measure the settle."""
    just_back = m("a", version=V)
    d = run([just_back, m("b")], [cmd("a", finished_ago=SETTLE_S - 10)])
    assert d.drain == [] and "settling" in d.waiting["p1"]
    assert run([just_back, m("b")], [cmd("a")]).drain == ["b"]


def test_never_take_the_last_healthy_member_down():
    d = run([m("a"), m("b", hb=400.0)])
    assert d.drain == [] and d.upgrade == [] and "unhealthy" in d.waiting["p1"]


def test_a_single_member_pool_is_upgraded_without_a_drain():
    """No partner to cover it: a short outage, allowed rather than never."""
    d = run([m("a")])
    assert d.upgrade == ["a"] and d.drain == []


def test_a_failed_or_expired_upgrade_halts_the_whole_rollout():
    d = run([ours("a"), m("c", "p2")], [cmd("a", "failed", "rolled back: no healthy heartbeat")],
            pools=("p1", "p2"))
    assert d.failed and "rolled back" in d.failed and d.upgrade == [] and d.drain == []
    assert run([m("a")], [cmd("a", "expired")]).failed


def test_done_when_every_member_runs_the_target_and_is_back():
    d = run([m("a", version=V), m("b", version=V)], [cmd("a"), cmd("b")])
    assert d.done and d.upgrade == [] and d.drain == []
    # The last member is still drained: not done until it is returned.
    assert not run([m("a", version=V), ours("b", version=V)], [cmd("a"), cmd("b")]).done


def test_an_operators_drain_is_never_touched_or_undone():
    d = run([m("a", state="draining", by="admin"), m("b")])
    assert d.upgrade == ["b"] and d.restore == [] and d.drain == []
    other = run([m("a", state="draining", by="rollout:someone-else"), m("b")])
    assert other.restore == [] and "a" not in other.drain
