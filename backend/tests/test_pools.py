"""Collector pools - pure: validation, aggregation arithmetic, the firewall
matrix, and the assignment ETag noticing a pool change. No database."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.schemas import Assignment, AssignmentBBMD, AssignmentPool
from app.services import pools as svc
from app.services.collector import etag_for

# ----------------------------------------------------------------- bbmd


def test_empty_bbmd_is_static_unicast():
    assert svc.validate_bbmd(None) == {}
    assert svc.validate_bbmd({}) == {}


def test_enabled_bbmd_needs_host_and_port():
    assert svc.validate_bbmd({"enabled": True, "bbmd": "10.52.1.1:47808"}) == {
        "enabled": True, "bbmd": "10.52.1.1:47808", "ttl_s": 300}
    for bad in ("10.52.1.1", ":47808", "10.52.1.1:0", "10.52.1.1:70000", 12):
        with pytest.raises(svc.PoolError):
            svc.validate_bbmd({"enabled": True, "bbmd": bad})


def test_bbmd_ttl_is_a_16_bit_wire_field_and_not_a_storm():
    assert svc.validate_bbmd({"enabled": True, "bbmd": "h:1", "ttl_s": 60})["ttl_s"] == 60
    for bad in (5, 70000, "soon"):
        with pytest.raises(svc.PoolError):
            svc.validate_bbmd({"enabled": True, "bbmd": "h:1", "ttl_s": bad})


def test_disabled_bbmd_keeps_the_address_for_re_enabling():
    got = svc.validate_bbmd({"enabled": False, "bbmd": "10.52.1.1:47808"})
    assert got == {"enabled": False, "bbmd": "10.52.1.1:47808", "ttl_s": 300}


def test_unknown_bbmd_keys_are_refused():
    with pytest.raises(svc.PoolError):
        svc.validate_bbmd({"enabled": True, "bbmd": "h:1", "bbdm": "typo"})


# ---------------------------------------------------------------- clean


def _create(**over):
    base = {"name": "DC1/BMS", "datacenter_id": "dc-1", "plane": "bms"}
    return {**base, **over}


def test_clean_create_requires_name_site_and_a_known_plane():
    out = svc.clean(_create(), partial=False)
    assert out == {"name": "DC1/BMS", "datacenter_id": "dc-1", "plane": "bms",
                   "cidrs": [], "trap_vip": None, "bbmd_settings": {},
                   "rate_budget_points_per_s": None, "min_members": 1,
                   "target_limits": {}}
    with pytest.raises(svc.PoolError):
        svc.clean(_create(name="  "), partial=False)
    with pytest.raises(svc.PoolError):
        svc.clean(_create(datacenter_id=None), partial=False)
    with pytest.raises(svc.PoolError):
        svc.clean(_create(plane="provider"), partial=False)


def test_clean_normalises_cidrs_and_refuses_junk():
    out = svc.clean(_create(cidrs=["10.52.1.5/24", " 10.52.2.0/24 "]), partial=False)
    assert out["cidrs"] == ["10.52.1.0/24", "10.52.2.0/24"]
    with pytest.raises(svc.PoolError):
        svc.clean(_create(cidrs=["not-a-cidr"]), partial=False)
    with pytest.raises(svc.PoolError):
        svc.clean(_create(cidrs="10.0.0.0/8"), partial=False)


def test_clean_trap_vip_must_be_an_address():
    assert svc.clean(_create(trap_vip="10.52.1.250"), partial=False)["trap_vip"] == "10.52.1.250"
    assert svc.clean(_create(trap_vip=""), partial=False)["trap_vip"] is None
    with pytest.raises(svc.PoolError):
        svc.clean(_create(trap_vip="10.52.1.0/24"), partial=False)


def test_clean_budget_and_min_members_bounds():
    assert svc.clean(_create(rate_budget_points_per_s=500, min_members=2),
                     partial=False)["min_members"] == 2
    with pytest.raises(svc.PoolError):
        svc.clean(_create(rate_budget_points_per_s=0), partial=False)
    with pytest.raises(svc.PoolError):
        svc.clean(_create(min_members=0), partial=False)


def test_clean_patch_only_touches_what_was_sent():
    assert svc.clean({"trap_vip": "10.52.1.250"}, partial=True) == {"trap_vip": "10.52.1.250"}
    assert svc.clean({}, partial=True) == {}


def test_clean_patch_cannot_move_a_pool_to_another_site_or_plane():
    for key in ("datacenter_id", "plane"):
        with pytest.raises(svc.PoolError):
            svc.clean({key: "x"}, partial=True)


# ------------------------------------------------------------ aggregate


def _pool(pid, site="DC1", plane="bms", min_members=1):
    return {"id": pid, "name": f"{site}/{plane}", "site": site, "plane": plane,
            "min_members": min_members, "cidrs": [], "bbmd_settings": {}}


def _member(cid, pool_id, healthy=True, accepting=True):
    return {"collector_id": cid, "pool_id": pool_id, "healthy": healthy,
            "accepting": accepting, "state": "active"}


def test_aggregate_counts_members_endpoints_owned_and_unassigned():
    out = svc.aggregate(
        pools=[_pool("p1"), _pool("p2", plane="it_oob")],
        members=[_member("c1", "p1"), _member("c2", "p1", healthy=False),
                 _member("c9", "nope")],
        endpoint_counts=[{"pool_id": "p1", "protocol": "bacnet", "n": 3},
                         {"pool_id": "p1", "protocol": "modbus", "n": 2},
                         {"pool_id": None, "protocol": "snmp", "n": 7}],
        ownable=[{"id": "e1", "pool_id": "p1"}, {"id": "e2", "pool_id": "p1"},
                 {"id": "e3", "pool_id": "p2"}, {"id": "e4", "pool_id": None}],
        plan={"e1": "c1", "e2": None, "e3": None, "e4": None},
    )
    p1, p2 = out["pools"][0], out["pools"][1]
    assert (p1["plane"], p2["plane"]) == ("bms", "it_oob")
    assert [m["collector_id"] for m in p1["members"]] == ["c1", "c2"]
    assert p1["healthy_members"] == 1 and p1["accepting_members"] == 1
    assert p1["endpoints"] == 5 and p1["protocols"] == {"bacnet": 3, "modbus": 2}
    assert p1["owned"] == 1 and p1["unassigned"] == 1
    assert p2["members"] == [] and p2["unassigned"] == 1
    assert out["unpooled"] == {"endpoints": 7, "protocols": {"snmp": 7}, "unassigned": 1}


def test_aggregate_below_min_members_only_once_an_operator_raised_it():
    out = svc.aggregate(pools=[_pool("p1", min_members=2), _pool("p2", plane="it_oob")],
                        members=[_member("c1", "p1")], endpoint_counts=[],
                        ownable=[], plan={})
    assert out["pools"][0]["below_min_members"] is True
    assert out["pools"][1]["below_min_members"] is False


# ------------------------------------------------------- firewall matrix


def _matrix(**pool_over):
    pool = {"id": "p1", "name": "DC1/BMS", "site": "DC1", "plane": "bms",
            "cidrs": [], "trap_vip": None, "bbmd_settings": {}, **pool_over}
    ranges = [{"cidr": "10.52.1.0/24", "enabled": True},
              {"cidr": "10.52.9.0/24", "enabled": False}]
    return svc.firewall_matrix(pool, ranges, {"bacnet": 4, "modbus": 2, "snmp": 1},
                               "https://dcim.example.com/")


def _rows(m, **match):
    return [r for r in m["rows"] if all(r[k] == v for k, v in match.items())]


def test_matrix_always_has_the_core_row_and_names_the_platform_host():
    core = _rows(_matrix(), protocol="core")
    assert len(core) == 1
    assert core[0] == {"direction": "outbound", "from": "collectors in pool DC1/BMS",
                       "to": "dcim.example.com", "transport": "tcp", "port": 443,
                       "protocol": "core",
                       "why": "assignment, config, credential and telemetry path to the core"}


def test_matrix_has_one_outbound_row_per_protocol_per_enabled_range():
    m = _matrix()
    assert _rows(m, protocol="bacnet") == [{
        "direction": "outbound", "from": "collectors in pool DC1/BMS",
        "to": "10.52.1.0/24", "transport": "udp", "port": 47808,
        "protocol": "bacnet", "why": "4 bacnet endpoint(s)"}]
    assert _rows(m, protocol="modbus")[0]["transport"] == "tcp"
    assert _rows(m, protocol="modbus")[0]["port"] == 502
    assert not [r for r in m["rows"] if r["to"] == "10.52.9.0/24"], "disabled range leaked"


def test_matrix_extra_pool_cidrs_are_destinations_too():
    m = _matrix(cidrs=["10.52.200.0/24"])
    assert {r["to"] for r in _rows(m, protocol="snmp")} == {"10.52.1.0/24", "10.52.200.0/24"}


def test_matrix_traps_go_to_the_vip_when_set_else_each_collector():
    with_vip = _rows(_matrix(trap_vip="10.52.1.250"), protocol="snmp_trap")
    assert with_vip == [{"direction": "inbound", "from": "10.52.1.0/24",
                         "to": "10.52.1.250", "transport": "udp", "port": 162,
                         "protocol": "snmp_trap", "why": "SNMP traps to the pool's trap VIP"}]
    without = _rows(_matrix(), protocol="snmp_trap")
    assert without[0]["to"] == "collectors in pool DC1/BMS"


def test_matrix_bbmd_row_only_when_enabled():
    assert _rows(_matrix(), protocol="bacnet_fdr") == []
    row = _rows(_matrix(bbmd_settings={"enabled": True, "bbmd": "10.52.1.1:47808",
                                       "ttl_s": 300}), protocol="bacnet_fdr")
    assert row == [{"direction": "outbound", "from": "collectors in pool DC1/BMS",
                    "to": "10.52.1.1", "transport": "udp", "port": 47808,
                    "protocol": "bacnet_fdr",
                    "why": "BACnet Foreign Device Registration with the pool's BBMD"}]


def test_matrix_redfish_implies_the_event_receiver_inbound():
    pool = {"id": "p", "name": "DC1/IT-OOB", "site": "DC1", "plane": "it_oob",
            "cidrs": [], "trap_vip": None, "bbmd_settings": {}}
    m = svc.firewall_matrix(pool, [{"cidr": "10.51.1.0/24", "enabled": True}],
                            {"redfish": 12}, None)
    ev = _rows(m, protocol="redfish_event")
    assert ev[0]["direction"] == "inbound" and ev[0]["port"] == 9143
    assert _rows(m, protocol="core")[0]["to"] == "<the DCIM platform>"
    assert _rows(m, protocol="snmp_trap") == []


def test_matrix_text_rendering_is_one_line_per_row():
    m = _matrix(trap_vip="10.52.1.250")
    lines = [ln for ln in m["text"].splitlines() if ln.strip()]
    assert lines[0].startswith("Firewall matrix - pool DC1/BMS (DC1/bms)")
    assert len(lines) == 1 + len(m["rows"])
    assert "inbound  10.52.1.0/24 -> 10.52.1.250  udp/162  [snmp_trap]" in m["text"]


# ------------------------------------------------------------------ etag


def _assignment(pools):
    return Assignment(version=3, generated_at=datetime(2026, 1, 1, tzinfo=UTC),
                      collector_id="col-1", site="DC1", endpoints=[], pools=pools)


def test_etag_changes_when_a_pools_bbmd_or_vip_changes_and_nothing_else_does():
    base = {"p1": AssignmentPool(id="p1", name="DC1/BMS", plane="bms")}
    bbmd = {"p1": AssignmentPool(id="p1", name="DC1/BMS", plane="bms",
                                 bbmd=AssignmentBBMD(enabled=True, bbmd="10.52.1.1:47808"))}
    vip = {"p1": AssignmentPool(id="p1", name="DC1/BMS", plane="bms", trap_vip="10.52.1.250")}
    tags = {etag_for(_assignment(p)) for p in (base, bbmd, vip, {})}
    assert len(tags) == 4, "a pool setting change must not answer 304"
    assert etag_for(_assignment(base)) == etag_for(_assignment(dict(base)))


def test_aggregate_carries_measured_load_against_the_budget():
    """docs/26 Phase 5: points/s measured from the pool's endpoints, the
    share of its rate budget that is, and its busiest live member."""
    p1 = {**_pool("p1"), "rate_budget_points_per_s": 400}
    p2 = _pool("p2", plane="it_oob")
    m1 = {**_member("c1", "p1"), "busy_pct": 40.0}
    m2 = {**_member("c2", "p1"), "busy_pct": 72.5}
    m3 = {**_member("c3", "p2"), "busy_pct": None}
    out = {p["id"]: p for p in svc.aggregate(
        [p1, p2], [m1, m2, m3], [], [], {}, {"p1": 340.04})["pools"]}
    assert out["p1"]["points_per_s"] == 340.0
    assert out["p1"]["budget_used_pct"] == 85.0
    assert out["p1"]["busiest_member_pct"] == 72.5
    # Unmeasured is None, never zero: nobody reports points for p2, and its
    # only member has not sent a capacity report.
    assert out["p2"]["points_per_s"] is None
    assert out["p2"]["budget_used_pct"] is None
    assert out["p2"]["busiest_member_pct"] is None


def test_a_measured_pool_with_no_budget_has_load_but_no_share():
    out = svc.aggregate([_pool("p1")], [], [], [], {}, {"p1": 12.0})["pools"][0]
    assert out["points_per_s"] == 12.0 and out["budget_used_pct"] is None


def test_clean_validates_target_limits_and_refuses_one_it_cannot_apply():
    out = svc.clean({"target_limits": {"modbus": {"max_concurrent": 1,
                                                  "min_interval_ms": 150}}}, partial=True)
    assert out == {"target_limits": {"modbus": {"max_concurrent": 1, "min_interval_ms": 150}}}
    with pytest.raises(svc.PoolError, match="max_concurrent"):
        svc.clean({"target_limits": {"modbus": {"max_concurrent": 0}}}, partial=True)
    with pytest.raises(svc.PoolError, match="not a polled protocol"):
        svc.clean({"target_limits": {"gnmi": {"max_concurrent": 1}}}, partial=True)
