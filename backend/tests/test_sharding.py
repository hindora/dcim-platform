"""Splitting a fleet across collectors.

The exit criterion is that two collectors split the fleet with no overlap, but
"no overlap" is only half of correct: a partition that drops endpoints has no
overlap either, and so does one that assigns everything to a collector which
cannot reach it.
"""

from __future__ import annotations

import uuid

from app.services import sharding as sh


def endpoints(n: int, site: str | None = "DC1",
              pinned: dict[int, str] | None = None) -> list[dict]:
    """Endpoints with stable ids, so a test can re-plan and compare."""
    pinned = pinned or {}
    out = []
    for i in range(n):
        out.append({
            "id": str(uuid.UUID(int=i + 1)),
            "site": site,
            "collector_id": pinned.get(i),
        })
    return out


def col(name: str, sites: tuple[str, ...] = (), pool_id: str | None = None,
        healthy: bool = True, accepting: bool = True) -> sh.Collector:
    return sh.Collector(collector_id=name, sites=frozenset(sites), pool_id=pool_id,
                        healthy=healthy, accepting=accepting)


def pool_endpoints(n: int, pool_id: str) -> list[dict]:
    return [{"id": str(uuid.UUID(int=i + 1)), "pool_id": pool_id, "site": None}
            for i in range(n)]


# --- the exit criterion -------------------------------------------------------

def test_two_collectors_split_the_fleet_with_no_overlap():
    eps = endpoints(1000)
    cols = [col("col-1"), col("col-2")]
    a = {e["id"] for e in sh.owned_by(eps, cols, "col-1")}
    b = {e["id"] for e in sh.owned_by(eps, cols, "col-2")}
    assert a & b == set()                      # no endpoint polled twice
    assert a | b == {e["id"] for e in eps}     # and none dropped
    assert a and b


def test_the_split_is_roughly_even():
    """Not a fairness requirement for its own sake - a collector with 90% of
    the fleet is the one that falls behind."""
    eps = endpoints(1000)
    cols = [col("col-1"), col("col-2")]
    counts = sh.distribution(sh.plan(eps, cols))
    for n in counts.values():
        assert 350 < n < 650


def test_a_single_collector_still_owns_everything():
    """The deployment that exists today must not change behaviour."""
    eps = endpoints(200)
    assert len(sh.owned_by(eps, [col("col-1")], "col-1")) == 200


def test_three_collectors_also_partition_cleanly():
    eps = endpoints(600)
    cols = [col("col-1"), col("col-2"), col("col-3")]
    owned = [{e["id"] for e in sh.owned_by(eps, cols, c.collector_id)} for c in cols]
    assert set.intersection(*owned) == set()
    assert set.union(*owned) == {e["id"] for e in eps}


# --- stability ----------------------------------------------------------------

def test_adding_a_collector_moves_about_one_share_and_no_more():
    """An endpoint that changes owner loses its counter baseline: the new
    collector has never seen it, so the next poll produces no rate at all.
    Modulo hashing would move nearly everything; rendezvous moves ~1/N."""
    eps = endpoints(1000)
    before = sh.plan(eps, [col("col-1"), col("col-2")])
    after = sh.plan(eps, [col("col-1"), col("col-2"), col("col-3")])
    moved = sh.movement(before, after)
    # A third collector should take roughly a third and disturb nothing else.
    assert 250 < moved < 420


def test_removing_a_collector_only_moves_its_own_endpoints():
    eps = endpoints(900)
    three = [col("col-1"), col("col-2"), col("col-3")]
    before = sh.plan(eps, three)
    after = sh.plan(eps, [col("col-1"), col("col-2")])
    orphaned = sum(1 for v in before.values() if v == "col-3")
    assert sh.movement(before, after) == orphaned


def test_assignment_does_not_depend_on_process_or_ordering():
    """Python's hash() is salted per process, so an assignment computed in one
    API worker would disagree with the next and endpoints would flap on every
    request. The order collectors arrive in must not matter either."""
    eps = endpoints(300)
    a = sh.plan(eps, [col("col-1"), col("col-2")])
    b = sh.plan(eps, [col("col-2"), col("col-1")])
    assert a == b
    assert sh.owner("fixed-id", None, "DC1", [col("x"), col("y")]) == \
        sh.owner("fixed-id", None, "DC1", [col("y"), col("x")])


# --- reachability -------------------------------------------------------------

def test_a_collector_is_never_given_a_site_it_cannot_reach():
    """The part pure hashing gets wrong. Management networks are per-site and
    frequently overlapping RFC1918; a collector that cannot route to a device
    cannot poll it, however balanced the hash is."""
    dc1 = endpoints(100, site="DC1")
    dc2 = endpoints(100, site="DC2")
    dc2 = [{**e, "id": f"dc2-{e['id']}"} for e in dc2]
    cols = [col("east", ("DC1",)), col("west", ("DC2",))]
    assert {e["id"] for e in sh.owned_by(dc1, cols, "east")} == {e["id"] for e in dc1}
    assert sh.owned_by(dc1, cols, "west") == []
    assert {e["id"] for e in sh.owned_by(dc2, cols, "west")} == {e["id"] for e in dc2}


def test_an_endpoint_no_collector_can_reach_is_reported_unassigned():
    """Not handed to someone who cannot poll it. Unpolled and visible beats
    assigned and silently failing."""
    eps = endpoints(10, site="DC3")
    cols = [col("east", ("DC1",)), col("west", ("DC2",))]
    assignment = sh.plan(eps, cols)
    assert set(assignment.values()) == {None}
    assert sh.distribution(assignment) == {"(unassigned)": 10}


def test_a_collector_with_no_declared_sites_serves_everything():
    """The single-site default, and why existing deployments are unaffected."""
    assert col("any").serves(None, "DC1")
    assert col("any").serves(None, None)
    assert not col("east", ("DC1",)).serves(None, "DC2")
    assert not col("east", ("DC1",)).serves(None, None)


# --- pins ---------------------------------------------------------------------

def test_a_pin_beats_the_hash():
    """A pin is an operator saying "this one, here" - a device only one
    collector can reach, or one being drained before maintenance."""
    eps = endpoints(100, pinned=dict.fromkeys(range(10), "col-2"))
    cols = [col("col-1"), col("col-2")]
    owned = {e["id"] for e in sh.owned_by(eps, cols, "col-2")}
    for e in eps[:10]:
        assert e["id"] in owned


def test_a_pin_to_an_unregistered_collector_is_kept_not_reassigned():
    """That collector may simply not have started yet. Moving its endpoints
    elsewhere in the meantime double-polls every one of them the moment it
    does."""
    eps = endpoints(50, pinned={0: "col-not-yet-started"})
    plan = sh.plan(eps, [col("col-1")])
    assert plan[eps[0]["id"]] == "col-not-yet-started"
    assert sh.owned_by(eps, [col("col-1")], "col-1") != []


def test_pins_do_not_break_the_no_overlap_guarantee():
    eps = endpoints(400, pinned=dict.fromkeys(range(0, 400, 7), "col-1"))
    cols = [col("col-1"), col("col-2")]
    a = {e["id"] for e in sh.owned_by(eps, cols, "col-1")}
    b = {e["id"] for e in sh.owned_by(eps, cols, "col-2")}
    assert a & b == set()
    assert a | b == {e["id"] for e in eps}


# --- pools (docs/26 Phase 5) ----------------------------------------------

def test_a_pool_placed_collector_ignores_sites_entirely():
    """pool_id is the whole placement decision once set - sites is not
    consulted, even if it happens to be populated too."""
    c = col("p", sites=("DC1",), pool_id="pool-a")
    assert c.serves("pool-a", None)
    assert c.serves("pool-a", "DC9")  # site is irrelevant once pool_id is set
    assert not c.serves("pool-b", "DC1")
    assert not c.serves(None, "DC1")


def test_adding_a_second_collector_to_a_pool_never_takes_another_pools_endpoint():
    a_eps = pool_endpoints(100, "pool-a")
    b_eps = pool_endpoints(100, "pool-b")
    cols = [col("a1", pool_id="pool-a"), col("a2", pool_id="pool-a"),
            col("b1", pool_id="pool-b")]
    a_owned = {e["id"] for e in sh.owned_by(a_eps, cols, "a1")} | \
        {e["id"] for e in sh.owned_by(a_eps, cols, "a2")}
    assert a_owned == {e["id"] for e in a_eps}
    assert sh.owned_by(b_eps, cols, "a1") == []
    assert sh.owned_by(b_eps, cols, "a2") == []


# --- rebalance damping (docs/26 Phase 5) ----------------------------------

def test_rebalance_does_nothing_below_the_deviation_threshold():
    """A large pool already spread across many members: one more joining
    changes the mean only slightly, and the existing members' current
    counts (~mean already) are nowhere near 10 over or 2x it. Must not
    reshuffle a pool this size over one new, still nearly-empty member."""
    eps = pool_endpoints(200, "pool-a")
    existing = [col(f"a{i}", pool_id="pool-a") for i in range(20)]
    joined = [*existing, col("a20", pool_id="pool-a")]
    current = sh.plan(eps, existing)  # ~10 each across 20 members
    target = sh.plan(eps, joined)     # ~9.5 each across 21 members
    result = sh.rebalance(current, target, eps, joined)
    assert result == current


def test_rebalance_triggers_exactly_at_the_boundary_not_only_past_it():
    """20 endpoints, solo -> pair: current is 20/0, which is EXACTLY
    deviation=10 and EXACTLY ratio=2x - the floors are both '>=', so this
    is meant to trigger, not the one case away from it."""
    eps = pool_endpoints(20, "pool-a")
    cols = [col("a1", pool_id="pool-a"), col("a2", pool_id="pool-a")]
    current = sh.plan(eps, [col("a1", pool_id="pool-a")])
    target = sh.plan(eps, cols)
    result = sh.rebalance(current, target, eps, cols)
    assert result == target


def test_rebalance_moves_everything_when_deviation_crosses_both_thresholds():
    eps = pool_endpoints(200, "pool-a")
    solo = [col("a1", pool_id="pool-a")]
    pair = [col("a1", pool_id="pool-a"), col("a2", pool_id="pool-a")]
    current = sh.plan(eps, solo)
    target = sh.plan(eps, pair)
    result = sh.rebalance(current, target, eps, pair)
    assert result == target
    assert sh.distribution(result)["a2"] > 0


def test_rebalance_moves_immediately_when_the_current_owner_is_draining():
    """docs/26 Phase 5's acceptance bar: a drain completes with zero
    polling gaps beyond one interval - the very next assigner run, not
    whatever the deviation threshold would otherwise require."""
    eps = pool_endpoints(20, "pool-a")
    healthy_pair = [col("a1", pool_id="pool-a"), col("a2", pool_id="pool-a")]
    current = sh.plan(eps, healthy_pair)
    draining = [col("a1", pool_id="pool-a", accepting=False),
               col("a2", pool_id="pool-a")]
    target = sh.plan(eps, draining)
    result = sh.rebalance(current, target, eps, draining)
    assert result == target
    assert "a1" not in sh.distribution(result)


def test_rebalance_does_not_move_anything_for_merely_unhealthy_on_its_own():
    """docs/26 Phase 0's own stated design, still true after Phase 6:
    "failover is deliberately NOT automatic" for a pool nobody gave real HA
    to. A stale heartbeat alone (healthy=False, accepting still True - no
    HA policy has run to turn that into accepting=False) must change
    nothing; see docs/26 Phase 6's apply_ha_policy for where automatic
    failover actually lives, opt-in per pool."""
    eps = pool_endpoints(20, "pool-a")
    healthy_pair = [col("a1", pool_id="pool-a"), col("a2", pool_id="pool-a")]
    current = sh.plan(eps, healthy_pair)
    one_unhealthy = [col("a1", pool_id="pool-a", healthy=False),
                     col("a2", pool_id="pool-a")]
    target = sh.plan(eps, one_unhealthy)
    result = sh.rebalance(current, target, eps, one_unhealthy)
    assert result == current


def test_rebalance_leaves_other_pools_untouched_by_one_pools_drain():
    a_eps = pool_endpoints(20, "pool-a")
    b_eps = pool_endpoints(20, "pool-b")
    eps = a_eps + b_eps
    healthy = [col("a1", pool_id="pool-a"), col("a2", pool_id="pool-a"),
              col("b1", pool_id="pool-b")]
    current = sh.plan(eps, healthy)
    a_drains = [col("a1", pool_id="pool-a", accepting=False),
               col("a2", pool_id="pool-a"), col("b1", pool_id="pool-b")]
    target = sh.plan(eps, a_drains)
    result = sh.rebalance(current, target, eps, a_drains)
    for e in b_eps:
        assert result[e["id"]] == current[e["id"]] == "b1"


def test_rebalance_assigns_a_brand_new_endpoint_straight_to_target():
    """No current entry at all - there is nothing to damp against, so the
    fresh target answer is simply adopted."""
    eps = pool_endpoints(5, "pool-a")
    cols = [col("a1", pool_id="pool-a")]
    current: dict[str, str | None] = {}
    target = sh.plan(eps, cols)
    result = sh.rebalance(current, target, eps, cols)
    assert result == target


def test_rebalance_result_covers_every_endpoint_given():
    eps = pool_endpoints(50, "pool-a")
    cols = [col("a1", pool_id="pool-a"), col("a2", pool_id="pool-a")]
    current = sh.plan(eps, cols)
    target = sh.plan(eps, cols)
    result = sh.rebalance(current, target, eps, cols)
    assert set(result) == {e["id"] for e in eps}


# --- HA policy (docs/26 Phase 6) -------------------------------------------

def ha_col(name: str, pool_id: str, heartbeat_age_s: float | None = 5.0,
          healthy_duration_s: float | None = 3600.0, accepting: bool = True) -> sh.Collector:
    return sh.Collector(collector_id=name, pool_id=pool_id, accepting=accepting,
                        heartbeat_age_s=heartbeat_age_s,
                        healthy_duration_s=healthy_duration_s)


def test_a_non_ha_pool_is_completely_unaffected():
    """min_members < 2 (the default) is Phase 0's original behaviour,
    verbatim - a stale heartbeat here must not flip accepting."""
    cols = [ha_col("a1", "pool-a", heartbeat_age_s=99999)]
    out = sh.apply_ha_policy(cols, {"pool-a": 1})
    assert out[0].accepting is True


def test_a_pool_with_no_min_members_entry_defaults_to_non_ha():
    cols = [ha_col("a1", "pool-a", heartbeat_age_s=99999)]
    out = sh.apply_ha_policy(cols, {})
    assert out[0].accepting is True


def test_a_draining_collector_stays_refused_regardless_of_health():
    cols = [ha_col("a1", "pool-a", accepting=False, heartbeat_age_s=1.0,
                   healthy_duration_s=99999)]
    out = sh.apply_ha_policy(cols, {"pool-a": 2})
    assert out[0].accepting is False


def test_a_stale_member_past_failover_loses_accepting_in_an_ha_pool():
    cols = [ha_col("a1", "pool-a", heartbeat_age_s=sh.FAILOVER_AFTER_S + 1)]
    out = sh.apply_ha_policy(cols, {"pool-a": 2})
    assert out[0].accepting is False


def test_a_member_just_stale_but_under_failover_still_accepts():
    """Between STALE_AFTER_S and FAILOVER_AFTER_S is the alarm window, not
    the failover window - Collector.healthy would already be False here,
    but accepting must not flip yet."""
    cols = [ha_col("a1", "pool-a", heartbeat_age_s=sh.FAILOVER_AFTER_S - 1)]
    out = sh.apply_ha_policy(cols, {"pool-a": 2})
    assert out[0].accepting is True


def test_an_empty_pools_first_member_is_never_quarantined():
    """No other healthy member exists to protect - a brand new HA pool must
    be able to do something immediately, not wait ten minutes for nothing."""
    cols = [ha_col("a1", "pool-a", healthy_duration_s=1.0)]
    out = sh.apply_ha_policy(cols, {"pool-a": 2})
    assert out[0].accepting is True


def test_a_recovering_member_is_quarantined_while_a_healthy_peer_exists():
    cols = [ha_col("a1", "pool-a", healthy_duration_s=1.0),   # just recovered
           ha_col("a2", "pool-a", healthy_duration_s=99999)]  # been fine all along
    out = {c.collector_id: c for c in sh.apply_ha_policy(cols, {"pool-a": 2})}
    assert out["a1"].accepting is False
    assert out["a2"].accepting is True


def test_a_recovering_member_regains_accepting_after_the_failback_window():
    cols = [ha_col("a1", "pool-a", healthy_duration_s=sh.FAILBACK_AFTER_S + 1),
           ha_col("a2", "pool-a", healthy_duration_s=99999)]
    out = {c.collector_id: c for c in sh.apply_ha_policy(cols, {"pool-a": 2})}
    assert out["a1"].accepting is True


def test_a_frozen_pool_never_fails_over_automatically():
    """docs/26 Phase 6: never during a change-freeze blackout."""
    cols = [ha_col("a1", "pool-a", heartbeat_age_s=sh.FAILOVER_AFTER_S + 1)]
    out = sh.apply_ha_policy(cols, {"pool-a": 2}, frozen_pools=frozenset({"pool-a"}))
    assert out[0].accepting is True


def test_a_frozen_pool_does_not_block_an_explicit_drain():
    """The freeze exemption only ever protects automatic failover - an
    operator's own drain decision still goes through."""
    cols = [ha_col("a1", "pool-a", accepting=False)]
    out = sh.apply_ha_policy(cols, {"pool-a": 2}, frozen_pools=frozenset({"pool-a"}))
    assert out[0].accepting is False


def test_ha_policy_end_to_end_failover_then_no_failback_for_ten_minutes():
    """The acceptance bar, in one test: stop the primary, its endpoints
    move to the standby past FAILOVER_AFTER_S; the primary comes back but
    nothing moves back until FAILBACK_AFTER_S has passed."""
    eps = pool_endpoints(50, "pool-a")
    both_up = [ha_col("primary", "pool-a"), ha_col("standby", "pool-a")]
    current = sh.plan(eps, sh.apply_ha_policy(both_up, {"pool-a": 2}))
    assert sh.distribution(current)["primary"] > 0

    primary_down = [ha_col("primary", "pool-a", heartbeat_age_s=sh.FAILOVER_AFTER_S + 1),
                    ha_col("standby", "pool-a")]
    policy_applied = sh.apply_ha_policy(primary_down, {"pool-a": 2})
    target = sh.plan(eps, policy_applied)
    after_failover = sh.rebalance(current, target, eps, policy_applied)
    assert sh.distribution(after_failover) == {"standby": 50}

    primary_back_but_recent = [
        ha_col("primary", "pool-a", heartbeat_age_s=1.0, healthy_duration_s=5.0),
        ha_col("standby", "pool-a")]
    policy_applied = sh.apply_ha_policy(primary_back_but_recent, {"pool-a": 2})
    target2 = sh.plan(eps, policy_applied)
    after_recovery = sh.rebalance(after_failover, target2, eps, policy_applied)
    assert sh.distribution(after_recovery) == {"standby": 50}, \
        "the primary must not reclaim anything inside the failback window"


def test_an_unplaced_collector_never_takes_a_pooled_endpoint():
    """docs/26 Phase 5: unassigned, never cross-pool. A pool with no
    healthy member leaves its endpoints owned by nobody - not by whichever
    serves-everything or same-site collector happens to exist."""
    eps = pool_endpoints(10, "pool-a")
    anywhere = col("anywhere")
    same_site = col("site-only", sites=("DC1",))
    assert not anywhere.serves("pool-a", "DC1")
    assert not same_site.serves("pool-a", "DC1")
    assert set(sh.plan(eps, [anywhere, same_site]).values()) == {None}
    # ...and an unpooled endpoint is still theirs, exactly as before pools.
    assert anywhere.serves(None, "DC1") and same_site.serves(None, "DC1")


# --- effective ownership: the record where valid, the plan where not ----------

def test_effective_with_no_record_is_the_live_plan():
    eps = pool_endpoints(30, "pool-a")
    cols = [col("a1", pool_id="pool-a"), col("a2", pool_id="pool-a")]
    assert sh.effective(eps, cols, {}) == sh.plan(eps, cols)


def test_a_valid_record_beats_the_plan_so_damping_reaches_the_collectors():
    """The assigner held an ordinary rebalance back; the serving path must
    honour that, or damping only ever changed a table."""
    eps = pool_endpoints(8, "pool-a")
    cols = [col("a1", pool_id="pool-a"), col("a2", pool_id="pool-a")]
    recorded = {e["id"]: "a1" for e in eps}
    assert set(sh.plan(eps, cols).values()) == {"a1", "a2"}, "HRW would split them"
    assert set(sh.effective(eps, cols, recorded).values()) == {"a1"}


def test_a_failover_in_the_record_reaches_the_collectors():
    """HA pools: the assigner moved these off a silent a1. The live plan
    (no HA policy) would still hand them to a1, which is accepting but dead."""
    eps = pool_endpoints(10, "pool-a")
    cols = [col("a1", pool_id="pool-a", healthy=False), col("a2", pool_id="pool-a")]
    recorded = {e["id"]: "a2" for e in eps}
    assert set(sh.effective(eps, cols, recorded).values()) == {"a2"}


def test_a_silent_owner_outside_ha_keeps_its_record():
    """Health is not a validity test: failover is opt-in per pool."""
    eps = pool_endpoints(5, "pool-a")
    cols = [col("a1", pool_id="pool-a", healthy=False), col("a2", pool_id="pool-a")]
    recorded = {e["id"]: "a1" for e in eps}
    assert set(sh.effective(eps, cols, recorded).values()) == {"a1"}


def test_a_stale_record_falls_back_to_the_plan():
    eps = pool_endpoints(10, "pool-a")
    live = [col("a2", pool_id="pool-a"), col("a3", pool_id="pool-a")]
    draining = [col("a1", pool_id="pool-a", accepting=False), *live]
    moved = [col("a1", pool_id="pool-b"), *live]
    recorded = {e["id"]: "a1" for e in eps}
    for fleet in (draining, moved, live):  # drained, moved pools, retired
        got = sh.effective(eps, fleet, recorded)
        assert "a1" not in got.values()
        assert got == sh.plan(eps, fleet)


def test_a_record_of_nobody_lets_the_plan_fill_it():
    eps = pool_endpoints(3, "pool-a")
    cols = [col("a1", pool_id="pool-a")]
    assert set(sh.effective(eps, cols, {e["id"]: None for e in eps}).values()) == {"a1"}


def test_a_pin_beats_the_record_at_once():
    eps = [{"id": "e1", "pool_id": "pool-a", "site": None, "collector_id": "a2"}]
    cols = [col("a1", pool_id="pool-a"), col("a2", pool_id="pool-a")]
    assert sh.effective(eps, cols, {"e1": "a1"}) == {"e1": "a2"}


def test_an_owner_that_no_longer_serves_the_endpoint_loses_it_at_once():
    """Found bringing up the six-collector fleet: a discovery range put 1426
    endpoints into pools, their unplaced owner could no longer serve one of
    them, and the record never moved - the damping loop only looks at a
    group's MEMBERS, and the old holder is not one."""
    eps = pool_endpoints(30, "pool-a")
    old_owner = col("anywhere")                    # unplaced, still accepting
    member = col("a1", pool_id="pool-a")
    current = {e["id"]: "anywhere" for e in eps}
    target = sh.plan(eps, [old_owner, member])
    assert set(target.values()) == {"a1"}
    got = sh.rebalance(current, target, eps, [old_owner, member])
    assert set(got.values()) == {"a1"}


def test_a_pin_survives_its_owner_losing_the_placement():
    eps = [{"id": "e1", "pool_id": "pool-a", "site": None, "collector_id": "anywhere"}]
    cols = [col("anywhere"), col("a1", pool_id="pool-a")]
    got = sh.rebalance({"e1": "anywhere"}, sh.plan(eps, cols), eps, cols)
    assert got == {"e1": "anywhere"}


def test_members_restarting_together_do_not_quarantine_each_other():
    """A host reboot, an upgrade or a stack restart brings every member of an
    HA pool back at once. Each saw the other as a healthy peer and held
    itself out, so the pool had no accepting member for the whole failback
    window - quarantine protects a long-healthy peer from a flapping one,
    and here there is no long-healthy peer to protect."""
    a = col("a1", pool_id="pool-a")
    b = col("a2", pool_id="pool-a")
    a.heartbeat_age_s, a.healthy_duration_s = 5.0, 40.0
    b.heartbeat_age_s, b.healthy_duration_s = 5.0, 35.0
    out = {c.collector_id: c.accepting for c in sh.apply_ha_policy([a, b], {"pool-a": 2})}
    assert out == {"a1": True, "a2": True}


def test_a_recovering_member_still_waits_beside_a_long_healthy_peer():
    back = col("a1", pool_id="pool-a")
    back.heartbeat_age_s, back.healthy_duration_s = 5.0, 40.0
    steady = col("a2", pool_id="pool-a")
    steady.heartbeat_age_s, steady.healthy_duration_s = 5.0, 7200.0
    out = {c.collector_id: c.accepting
           for c in sh.apply_ha_policy([back, steady], {"pool-a": 2})}
    assert out == {"a1": False, "a2": True}
