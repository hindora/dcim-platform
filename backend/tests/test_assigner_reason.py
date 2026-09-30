"""assigner._reason, pure: what a recorded move is called in the history."""

from __future__ import annotations

from app.services.assigner import _reason
from app.services.sharding import Collector

EP = {"id": "e1", "pool_id": "pool-a", "site": "DC1", "collector_id": None}


def test_an_owner_that_no_longer_serves_the_endpoint_is_a_placement_move():
    fleet = {"anywhere": Collector("anywhere", pool_id="pool-b"),
             "a1": Collector("a1", pool_id="pool-a")}
    assert _reason("anywhere", "a1", EP, fleet) == "placement"


def test_a_draining_owner_is_still_a_drain_not_a_placement():
    fleet = {"old": Collector("old", pool_id="pool-b", accepting=False),
             "a1": Collector("a1", pool_id="pool-a")}
    assert _reason("old", "a1", EP, fleet) == "drain"


def test_an_eligible_owner_losing_to_the_hash_is_a_rebalance():
    fleet = {"a0": Collector("a0", pool_id="pool-a"), "a1": Collector("a1", pool_id="pool-a")}
    assert _reason("a0", "a1", EP, fleet) == "rebalance"
