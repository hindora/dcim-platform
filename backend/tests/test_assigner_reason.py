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


def test_leaving_a_failover_holder_for_a_healthy_member_is_a_failback():
    fleet = {"a1": Collector("a1", pool_id="pool-a"), "a2": Collector("a2", pool_id="pool-a")}
    assert _reason("a2", "a1", EP, fleet, current_reason="failover") == "failback"


def test_the_failover_itself_is_still_a_failover():
    fleet = {"a1": Collector("a1", pool_id="pool-a", heartbeat_age_s=400.0),
             "a2": Collector("a2", pool_id="pool-a")}
    assert _reason("a1", "a2", EP, fleet, current_reason="rebalance") == "failover"


def test_an_ordinary_move_off_an_ordinary_holder_is_still_a_rebalance():
    fleet = {"a1": Collector("a1", pool_id="pool-a"), "a2": Collector("a2", pool_id="pool-a")}
    assert _reason("a2", "a1", EP, fleet, current_reason="initial") == "rebalance"
