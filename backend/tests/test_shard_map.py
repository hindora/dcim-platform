"""The shard map and drain preview (docs/26 Phase 5), pure: fixed rows in,
no database. The drain preview is computed with the same sharding.plan the
serving path uses, so these pin down what an operator is shown BEFORE they
move anything."""

from __future__ import annotations

from app.services.shard_map import build_rows, filter_rows, preview_drain, summarise
from app.services.sharding import Collector


def ep(eid, pool=None, site="DC1", pinned=None):
    return {"id": eid, "pool_id": pool, "site": site, "collector_id": pinned}


def disp(eid, name=None, proto="snmp", address=None, recorded=None, reason=None,
         pinned=None):
    return {"id": eid, "device_id": f"d-{eid}", "device_name": name or f"dev-{eid}",
            "device_type": "ups", "protocol": proto, "address": address,
            "pinned_to": pinned, "recorded_owner": recorded, "epoch": 1 if reason else None,
            "since": None, "reason": reason}


# ------------------------------------------------------------------ shard map

def test_rows_show_served_and_recorded_owners_side_by_side():
    rows = build_rows(
        [disp("e1", recorded="c1", reason="initial"),
         disp("e2", recorded="c1", reason="initial"),
         disp("e3")],
        [ep("e1", pool="p1"), ep("e2", pool="p1"), ep("e3")],
        {"e1": "c1", "e2": "c2", "e3": "c1"}, {"p1": "DC1/BMS"})
    by = {r["id"]: r for r in rows}
    assert by["e1"]["owner"] == "c1" and not by["e1"]["record_disagrees"]
    assert by["e2"]["owner"] == "c2" and by["e2"]["record_disagrees"], \
        "served c2 but recorded c1 - the damping case, shown not hidden"
    assert by["e3"]["record_disagrees"] is False, "no record yet is not a disagreement"
    assert by["e1"]["pool_name"] == "DC1/BMS" and by["e3"]["pool_name"] is None


def test_summary_counts_owned_pinned_unassigned_and_unknown_pins():
    rows = build_rows(
        [disp("e1"), disp("e2", pinned="c1"), disp("e3"), disp("e4", pinned="ghost")],
        [ep("e1"), ep("e2", pinned="c1"), ep("e3"), ep("e4", pinned="ghost")],
        {"e1": "c1", "e2": "c1", "e3": None, "e4": "ghost"}, {})
    s = summarise(rows, [Collector("c1"), Collector("c2")])
    by = {c["collector_id"]: c for c in s["by_collector"]}
    assert by["c1"]["owned"] == 2 and by["c1"]["pinned"] == 1
    assert by["c2"]["owned"] == 0
    assert s["unassigned"] == 1
    # A pin to a collector outside the fleet is kept by the plan and polled
    # by nothing - it must be on the map, not silently missing.
    assert by["ghost"]["not_in_fleet"] is True and by["ghost"]["owned"] == 1
    assert s["unrecorded"] == 4


def test_filters_compose():
    rows = build_rows(
        [disp("e1", name="UPS-A", proto="snmp", address="10.51.1.5"),
         disp("e2", name="CHILLER-1", proto="bacnet", address="10.52.1.9",
              recorded="c9", reason="drain"),
         disp("e3", name="UPS-B", proto="snmp")],
        [ep("e1", pool="p1"), ep("e2", pool="p2"), ep("e3", pool="p1")],
        {"e1": "c1", "e2": "c2", "e3": None}, {})
    ids = lambda **f: [r["id"] for r in filter_rows(rows, **f)]  # noqa: E731
    assert ids(collector_id="c1") == ["e1"]
    assert ids(pool_id="p1") == ["e1", "e3"]
    assert ids(protocol="bacnet") == ["e2"]
    assert ids(q="ups") == ["e1", "e3"]
    assert ids(q="10.52.") == ["e2"]
    assert ids(disagree_only=True) == ["e2"]
    assert ids(unassigned_only=True) == ["e3"]


# ---------------------------------------------------------------------- drain

def test_drain_moves_unpinned_work_to_the_rest_of_its_pool():
    endpoints = [ep(f"e{i}", pool="p1") for i in range(40)]
    fleet = [Collector("c1", pool_id="p1"), Collector("c2", pool_id="p1"),
             Collector("c3", pool_id="p2")]
    got = preview_drain("c1", endpoints, fleet)
    assert got["owned"] > 0
    assert got["moving"] == got["owned"]
    assert set(got["destinations"]) == {"c2"}, "never to another pool's collector"
    assert got["stranded"] == 0 and got["pinned"] == 0
    assert got["can_drain"] is True and got["blockers"] == []


def test_pins_stay_and_are_counted():
    endpoints = [ep("e1", pool="p1", pinned="c1"), ep("e2", pool="p1")]
    fleet = [Collector("c1", pool_id="p1"), Collector("c2", pool_id="p1")]
    got = preview_drain("c1", endpoints, fleet)
    assert got["pinned"] == 1
    assert got["owned"] == got["pinned"] + got["moving"]


def test_draining_the_last_member_of_a_pool_is_blocked():
    """The Kubernetes-drain guard: refusing is right when the alternative is
    a pool polled by nothing, and the number is what the operator reads."""
    endpoints = [ep(f"e{i}", pool="p1") for i in range(5)]
    fleet = [Collector("c1", pool_id="p1"), Collector("c2", pool_id="p2")]
    got = preview_drain("c1", endpoints, fleet)
    assert got["stranded"] == 5
    assert got["stranded_pools"] == {"p1": 5}
    assert got["can_drain"] is False
    assert "nowhere to go" in got["blockers"][0]


def test_a_collector_owning_nothing_drains_to_nothing():
    got = preview_drain("c9", [ep("e1")], [Collector("c1"), Collector("c9", pool_id="px")])
    assert got == {**got, "owned": 0, "moving": 0, "stranded": 0, "can_drain": True}


def test_an_unpooled_collector_hands_work_to_its_site():
    endpoints = [ep(f"e{i}", site="DC1") for i in range(30)]
    fleet = [Collector("c1", sites=frozenset({"DC1"})),
             Collector("c2", sites=frozenset({"DC1"})),
             Collector("c3", sites=frozenset({"DC2"}))]
    got = preview_drain("c1", endpoints, fleet)
    assert set(got["destinations"]) <= {"c2"}
    assert got["stranded"] == 0


# ---------------------------------------------------------------- rebalance

def _plan(current, result, pools, changes=None, frozen=()):
    from types import SimpleNamespace
    changes = changes if changes is not None else {
        e: (result[e], "rebalance") for e in result if result[e] != current.get(e)}
    return SimpleNamespace(current=current, result=result, changes=changes,
                           endpoints_by_id={e: {"pool_id": p} for e, p in pools.items()},
                           frozen_pools=frozenset(frozen), collectors=[])


def test_rebalance_preview_counts_only_its_own_pool():
    from app.services.shard_map import summarise_rebalance
    pools = {"e1": "pa", "e2": "pa", "e3": "pa", "e4": "pa", "x1": "pb"}
    current = {"e1": "a1", "e2": "a1", "e3": "a1", "e4": "a1", "x1": "b1"}
    forced = _plan(current, {**current, "e2": "a2", "e4": "a2", "x1": "b1"}, pools)
    auto = _plan(current, dict(current), pools)
    got = summarise_rebalance("pa", forced, auto)
    assert got["endpoints"] == 4 and got["moving"] == 2
    assert got["moves"] == [{"from": "a1", "to": "a2", "count": 2}]
    assert got["before"] == {"a1": 4} and got["after"] == {"a1": 2, "a2": 2}
    assert got["automatic"] == 0, "damping was holding all of it"
    assert got["balanced"] is False and got["frozen"] is False


def test_a_balanced_or_frozen_pool_says_so():
    from app.services.shard_map import summarise_rebalance
    pools = {"e1": "pa", "e2": "pa"}
    current = {"e1": "a1", "e2": "a2"}
    p = _plan(current, dict(current), pools, frozen=("pa",))
    got = summarise_rebalance("pa", p, p)
    assert got["balanced"] is True and got["moving"] == 0 and got["frozen"] is True
