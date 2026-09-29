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


def test_rebalance_moves_immediately_when_the_current_owner_is_unhealthy():
    eps = pool_endpoints(20, "pool-a")
    healthy_pair = [col("a1", pool_id="pool-a"), col("a2", pool_id="pool-a")]
    current = sh.plan(eps, healthy_pair)
    one_down = [col("a1", pool_id="pool-a", healthy=False),
               col("a2", pool_id="pool-a")]
    target = sh.plan(eps, one_down)
    result = sh.rebalance(current, target, eps, one_down)
    assert result == target


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
