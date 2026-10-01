"""Collector pools against the real dev database, transaction rolled back.

The same fixture shape as test_assigner_live.py, for the same reason: pool
resolution is a CIDR containment query joined across four tables, and only
Postgres can tell you whether it does what the Python thinks it does.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.repositories import collector as fleet_repo
from app.repositories import pools as repo
from app.services import collector as fleet
from app.services import pools as svc

DB_URL = os.environ.get("DCIM_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(DB_URL, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        trans = await s.begin()
        try:
            yield s
        finally:
            await trans.rollback()
            await engine.dispose()


def _tag() -> str:
    return uuid.uuid4().hex[:8]


async def _datacenter_and_room(session, code: str) -> tuple[str, str]:
    dc_id = await session.scalar(text("""
        INSERT INTO datacenter (code, name) VALUES (:c, :n) RETURNING id::text
    """), {"c": code, "n": code})
    room_id = await session.scalar(text("""
        INSERT INTO room (datacenter_id, name) VALUES (CAST(:dc AS uuid), 'main')
        RETURNING id::text
    """), {"dc": dc_id})
    return dc_id, room_id


async def _discovery_range(session, dc_id: str, cidr: str, purpose: str = "bms") -> None:
    await session.execute(text("""
        INSERT INTO discovery_range (cidr, name, datacenter_id, purpose)
        VALUES (CAST(:cidr AS cidr), :cidr, CAST(:dc AS uuid), :purpose)
    """), {"cidr": cidr, "dc": dc_id, "purpose": purpose})


async def _poll_profile(session) -> str:
    return await session.scalar(text("""
        INSERT INTO poll_profile (name, interval_s, timeout_ms, retries, metric_groups)
        VALUES (:n, 30, 3000, 2, '{}') RETURNING id::text
    """), {"n": f"profile-{_tag()}"})


async def _device_endpoint(session, room_id: str, profile_id: str, address: str,
                           protocol: str = "bacnet") -> str:
    device_id = await session.scalar(text("""
        INSERT INTO device (name, device_type, lifecycle, room_id)
        VALUES (:n, 'chiller', 'in_service', CAST(:room AS uuid)) RETURNING id::text
    """), {"n": f"dev-{_tag()}", "room": room_id})
    return await session.scalar(text("""
        INSERT INTO device_endpoint (device_id, protocol, role, address, poll_profile_id,
                                     enabled, admin_state)
        VALUES (CAST(:d AS uuid), CAST(:p AS protocol_t), 'native_card',
                CAST(:addr AS inet), CAST(:profile AS uuid), true, 'enabled')
        RETURNING id::text
    """), {"d": device_id, "p": protocol, "addr": address, "profile": profile_id})


async def _collector(session, collector_id: str, pool_id: str | None) -> None:
    await session.execute(text("""
        INSERT INTO collector_instance (id, last_heartbeat, endpoints_owned,
                                        endpoints_online, status, stats, state,
                                        token_generation, state_changed_at,
                                        state_changed_by, pool_id, started_at)
        VALUES (:id, now(), 0, 0, 'HEALTHY', '{}'::jsonb, 'active', 1, now(), 'test',
                CAST(:pool AS uuid), now())
    """), {"id": collector_id, "pool": pool_id})


def _payload(dc_id: str, **over):
    return {"name": "DC/BMS", "datacenter_id": dc_id, "plane": "bms",
            "cidrs": ["10.52.200.0/24"], "trap_vip": "10.52.1.250",
            "bbmd_settings": {"enabled": True, "bbmd": "10.52.1.1:47808", "ttl_s": 120},
            "rate_budget_points_per_s": 400, "min_members": 2, **over}


async def test_create_and_read_back_every_column(session):
    dc_id, _ = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id))
    got = await repo.get_pool(session, pool["id"])
    assert got["name"] == "DC/BMS" and got["plane"] == "bms"
    assert got["cidrs"] == ["10.52.200.0/24"]
    assert got["trap_vip"] == "10.52.1.250"
    assert got["bbmd_settings"] == {"enabled": True, "bbmd": "10.52.1.1:47808", "ttl_s": 120}
    assert got["rate_budget_points_per_s"] == 400 and got["min_members"] == 2
    assert got["datacenter_id"] == dc_id


async def test_one_pool_per_site_and_plane(session):
    dc_id, _ = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    await svc.create(session, _payload(dc_id))
    with pytest.raises(svc.PoolConflictError):
        await svc.create(session, _payload(dc_id, name="another"))
    # ...but the same plane at another site is fine.
    other, _ = await _datacenter_and_room(session, f"Q{_tag()[:6]}")
    assert await svc.create(session, _payload(other))


async def test_unknown_site_is_a_pool_error_not_a_500(session):
    with pytest.raises(svc.PoolError):
        await svc.create(session, _payload(str(uuid.uuid4())))


async def test_update_writes_only_what_changed_and_bumps_updated_at(session):
    dc_id, _ = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id))
    before_ts = pool["updated_at"]

    assert await svc.update(session, pool["id"], {"trap_vip": "10.52.1.250"}) == ({}, {})

    before, after = await svc.update(session, pool["id"],
                                     {"bbmd_settings": {}, "min_members": 1})
    assert before == {"bbmd_settings": {"enabled": True, "bbmd": "10.52.1.1:47808",
                                        "ttl_s": 120}, "min_members": 2}
    assert after == {"bbmd_settings": {}, "min_members": 1}
    got = await repo.get_pool(session, pool["id"])
    assert got["bbmd_settings"] == {} and got["min_members"] == 1
    assert got["updated_at"] >= before_ts


async def test_endpoint_in_a_matching_range_resolves_into_the_pool(session):
    dc_id, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[]))
    await _discovery_range(session, dc_id, "10.52.7.0/24", purpose="bms")
    await _discovery_range(session, dc_id, "10.51.7.0/24", purpose="it_oob")  # no pool
    profile = await _poll_profile(session)
    await _device_endpoint(session, room_id, profile, "10.52.7.10", "bacnet")
    await _device_endpoint(session, room_id, profile, "10.52.7.11", "modbus")
    await _device_endpoint(session, room_id, profile, "10.51.7.10", "snmp")

    counts = {(c["pool_id"], c["protocol"]): c["n"]
              for c in await repo.endpoint_counts(session)}
    assert counts[(pool["id"], "bacnet")] == 1
    assert counts[(pool["id"], "modbus")] == 1
    assert (pool["id"], "snmp") not in counts, "an it_oob range must not resolve to the bms pool"

    ranges = await repo.ranges_for(session, pool["id"])
    assert [r["cidr"] for r in ranges] == ["10.52.7.0/24"]


async def test_an_explicit_endpoint_pool_id_beats_the_range(session):
    dc_id, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    bms = await svc.create(session, _payload(dc_id, cidrs=[]))
    oob = await svc.create(session, _payload(dc_id, name="DC/OOB", plane="it_oob",
                                             cidrs=[], bbmd_settings={}))
    await _discovery_range(session, dc_id, "10.52.8.0/24", purpose="bms")
    profile = await _poll_profile(session)
    ep = await _device_endpoint(session, room_id, profile, "10.52.8.10", "snmp")
    await session.execute(text("""
        UPDATE device_endpoint SET pool_id = CAST(:p AS uuid) WHERE id = CAST(:e AS uuid)
    """), {"p": oob["id"], "e": ep})

    counts = {(c["pool_id"], c["protocol"]): c["n"]
              for c in await repo.endpoint_counts(session)}
    assert counts.get((oob["id"], "snmp")) == 1
    assert (bms["id"], "snmp") not in counts


async def test_delete_is_refused_while_a_collector_is_placed_in_the_pool(session):
    dc_id, _ = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id))
    await _collector(session, f"col-{_tag()}", pool["id"])
    with pytest.raises(svc.PoolError, match="placed in this pool"):
        await svc.delete(session, pool["id"])

    await session.execute(text("UPDATE collector_instance SET pool_id = NULL "
                               "WHERE pool_id = CAST(:p AS uuid)"), {"p": pool["id"]})
    await svc.delete(session, pool["id"])
    assert await repo.get_pool(session, pool["id"]) is None


async def test_members_and_overview_see_the_placed_collector(session):
    dc_id, _ = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id))
    cid = f"col-{_tag()}"
    await _collector(session, cid, pool["id"])

    members = [m for m in await repo.members(session) if m["pool_id"] == pool["id"]]
    assert [m["collector_id"] for m in members] == [cid]
    assert members[0]["healthy"] and members[0]["accepting"]

    detail = await svc.detail(session, pool["id"])
    assert detail["healthy_members"] == 1 and detail["accepting_members"] == 1
    # min_members=2 with one healthy member: the same condition the
    # pool_below_min_members alarm fires on.
    assert detail["below_min_members"] is True


async def test_the_assignment_carries_the_pools_settings_to_a_placed_collector(session):
    dc_id, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[]))
    await _discovery_range(session, dc_id, "10.52.9.0/24", purpose="bms")
    profile = await _poll_profile(session)
    ep = await _device_endpoint(session, room_id, profile, "10.52.9.10", "bacnet")
    cid = f"col-{_tag()}"
    await _collector(session, cid, pool["id"])
    # Pinned - see test_preflight_targets_for_an_active_pool_member_are_what_it_owns.
    await session.execute(text("UPDATE device_endpoint SET collector_id = :c "
                               "WHERE id = CAST(:e AS uuid)"), {"c": cid, "e": ep})

    assignment = await fleet.build_assignment(session, cid)
    mine = [e for e in assignment.endpoints if e.id == ep]
    assert mine and mine[0].pool_id == pool["id"]
    assert set(assignment.pools) == {pool["id"]}
    carried = assignment.pools[pool["id"]]
    assert carried.bbmd.enabled and carried.bbmd.bbmd == "10.52.1.1:47808"
    assert carried.bbmd.ttl_s == 120 and carried.trap_vip == "10.52.1.250"
    assert carried.rate_budget_points_per_s == 400


async def test_a_pool_placed_collector_with_no_endpoints_still_gets_its_pool(session):
    """So a BBMD registration starts before the first endpoint is discovered."""
    dc_id, _ = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[]))
    cid = f"col-{_tag()}"
    await _collector(session, cid, pool["id"])
    assignment = await fleet.build_assignment(session, cid)
    assert set(assignment.pools) == {pool["id"]}


async def test_preflight_targets_fall_back_to_the_pool_for_a_collector_owning_nothing(session):
    """The just-enrolled case: a pending collector owns nothing yet, but its
    pool says what it will own - that is what preflight must probe."""
    dc_id, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[]))
    await _discovery_range(session, dc_id, "10.52.11.0/24", purpose="bms")
    profile = await _poll_profile(session)
    await _device_endpoint(session, room_id, profile, "10.52.11.10", "bacnet")
    await _device_endpoint(session, room_id, profile, "10.52.11.20", "modbus")
    cid = f"col-{_tag()}"
    await _collector(session, cid, pool["id"])
    await session.execute(text("UPDATE collector_instance SET state = 'pending' "
                               "WHERE id = :id"), {"id": cid})

    got = await fleet.preflight_targets(session, cid)
    assert got["source"] == "pool"
    by_proto = {p["protocol"]: p["targets"] for p in got["protocols"]}
    assert by_proto["bacnet"] == [{"address": "10.52.11.10", "port": 47808}]
    assert by_proto["modbus"] == [{"address": "10.52.11.20", "port": 502}]


async def test_preflight_targets_for_an_active_pool_member_are_what_it_owns(session):
    dc_id, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[]))
    await _discovery_range(session, dc_id, "10.52.12.0/24", purpose="bms")
    profile = await _poll_profile(session)
    ep = await _device_endpoint(session, room_id, profile, "10.52.12.10", "bacnet")
    cid = f"col-{_tag()}"
    await _collector(session, cid, pool["id"])
    # Pinned: the real dev fleet's unplaced collector serves everything and
    # competes by rendezvous hash for a pool endpoint, so without a pin the
    # owner depends on a random UUID - the trap docs/26 Phase 5 documented.
    await session.execute(text("UPDATE device_endpoint SET collector_id = :c "
                               "WHERE id = CAST(:e AS uuid)"), {"c": cid, "e": ep})

    got = await fleet.preflight_targets(session, cid)
    assert got["source"] == "owned"
    assert [p["protocol"] for p in got["protocols"]] == ["bacnet"]


async def test_preflight_targets_are_none_with_nothing_owned_and_no_pool(session):
    cid = f"col-{_tag()}"
    await _collector(session, cid, None)
    await session.execute(text("UPDATE collector_instance SET state = 'pending' "
                               "WHERE id = :id"), {"id": cid})
    got = await fleet.preflight_targets(session, cid)
    assert got == {"collector_id": cid, "source": "none", "protocols": []}


async def test_set_pool_keeps_the_collectors_site_in_step(session):
    dc_id, _ = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id))
    cid = f"col-{_tag()}"
    await _collector(session, cid, None)
    await fleet_repo.set_pool(session, cid, pool["id"])
    row = await fleet_repo.collector_state(session, cid)
    assert row["pool_id"] == pool["id"] and row["datacenter_id"] == dc_id
    await fleet_repo.set_pool(session, cid, None)
    row = await fleet_repo.collector_state(session, cid)
    assert row["pool_id"] is None and row["datacenter_id"] == dc_id, \
        "leaving a pool keeps the site it implied"


async def test_collector_detail_shows_owned_endpoints_and_skew(session):
    dc_id, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[]))
    await _discovery_range(session, dc_id, "10.52.13.0/24", purpose="bms")
    profile = await _poll_profile(session)
    ep = await _device_endpoint(session, room_id, profile, "10.52.13.10", "bacnet")
    cid = f"col-{_tag()}"
    await _collector(session, cid, pool["id"])
    # Pinned: the real dev fleet's unplaced collector serves everything and
    # competes by rendezvous hash for a pool endpoint, so without a pin the
    # owner depends on a random UUID - the trap docs/26 Phase 5 documented.
    await session.execute(text("UPDATE device_endpoint SET collector_id = :c "
                               "WHERE id = CAST(:e AS uuid)"), {"c": cid, "e": ep})

    detail = await fleet.collector_detail(session, cid)
    assert detail["id"] == cid and detail["pool_id"] == pool["id"]
    assert [e["id"] for e in detail["endpoints"]] == [ep]
    assert detail["by_protocol"] == {"bacnet": 1}
    assert detail["version_skew"] == "unknown", "a collector that never reported a build"
    assert "config" not in detail and "effective" not in detail
    assert await fleet.collector_detail(session, f"nope-{_tag()}") is None


async def test_readiness_counts_credentials_state_and_member_preflight(session):
    """The wizard's credentials and verification steps read these numbers;
    each must come from the row it names, not from a guess."""
    from app.repositories import preflight as preflight_repo

    dc_id, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[], plane="it_oob",
                                              min_members=1, bbmd_settings={}))
    await _discovery_range(session, dc_id, "10.51.13.0/24", purpose="it_oob")
    profile = await _poll_profile(session)
    with_cred = await _device_endpoint(session, room_id, profile, "10.51.13.10", "snmp")
    await _device_endpoint(session, room_id, profile, "10.51.13.11", "snmp")
    await _device_endpoint(session, room_id, profile, "10.51.13.12", "modbus")
    cred = await session.scalar(text("""
        INSERT INTO credential (name, protocol, kind, secret_enc)
        VALUES (:n, 'snmp', 'snmp_v2c', 'x') RETURNING id::text
    """), {"n": f"cred-{_tag()}"})
    await session.execute(text("UPDATE device_endpoint SET credential_id = CAST(:c AS uuid) "
                               "WHERE id = CAST(:e AS uuid)"), {"c": cred, "e": with_cred})
    await session.execute(text("""
        INSERT INTO endpoint_state (endpoint_id, status, updated_at)
        VALUES (CAST(:e AS uuid), 'ONLINE', now())
    """), {"e": with_cred})
    cid = f"col-{_tag()}"
    await _collector(session, cid, pool["id"])
    await preflight_repo.record(session, cid, False, [{"check": "ntp_offset", "status": "fail"}])

    got = await svc.readiness(session, pool["id"])
    by = {p["protocol"]: p for p in got["protocols"]}
    assert by["snmp"]["endpoints"] == 2
    assert by["snmp"]["with_credential"] == 1
    assert by["snmp"]["missing_credential"] == 1
    assert by["snmp"]["online"] == 1 and by["snmp"]["never_polled"] == 1
    assert by["modbus"]["needs_credential"] is False
    assert by["modbus"]["missing_credential"] == 0, "Modbus/TCP has no auth to be missing"
    assert got["members"][0]["collector_id"] == cid
    assert got["members"][0]["preflight_passed"] is False
    checks = {c["key"]: c["ok"] for c in got["checks"]}
    assert checks["preflight"] is False
    assert checks["credentials"] is False
    assert checks["online"] is False
    assert got["ready"] is False
    assert [r["cidr"] for r in got["ranges"]] == ["10.51.13.0/24"]


async def test_the_monitor_reads_capacity_and_per_pool_points_from_heartbeats(session):
    """docs/26 Phase 5: busy % per collector, and a pool's measured points/s
    summed over every live collector reporting it - member or not."""
    import json

    from app.alarms import platform_monitor as mon

    dc_id, _ = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[]))  # budget 400
    member, outsider = f"col-{_tag()}", f"col-{_tag()}"
    await _collector(session, member, pool["id"])
    await _collector(session, outsider, None)
    for cid, pts, pct in ((member, 300.0, 91.5), (outsider, 50.0, 10.0)):
        await session.execute(text("""
            UPDATE collector_instance SET stats = CAST(:s AS jsonb) WHERE id = :id
        """), {"id": cid, "s": json.dumps({"capacity": {
            "window_s": 300, "busy_pct": pct, "shed": 0, "late": 2,
            "pools": {pool["id"]: {"points_per_s": pts}}}})})

    pools = {p.pool_id: p for p in await mon._pools(session)}
    got = pools[pool["id"]]
    assert got.rate_budget_points_per_s == 400.0
    assert got.points_per_s == 350.0, "a pinned endpoint's points count wherever they came from"

    cols = {c.collector_id: c for c in await mon._collectors(session)}
    assert cols[member].capacity_busy_pct == 91.5
    assert cols[member].capacity_window_s == 300.0
    assert cols[member].capacity_late == 2


async def test_a_pool_nobody_reports_points_for_is_unmeasured_not_zero(session):
    from app.alarms import platform_monitor as mon

    dc_id, _ = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[]))
    pools = {p.pool_id: p for p in await mon._pools(session)}
    assert pools[pool["id"]].points_per_s is None


async def test_pool_points_and_member_busy_reach_the_pool_view(session):
    import json

    dc_id, _ = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[]))  # budget 400
    cid = f"col-{_tag()}"
    await _collector(session, cid, pool["id"])
    await session.execute(text("""
        UPDATE collector_instance SET stats = CAST(:s AS jsonb) WHERE id = :id
    """), {"id": cid, "s": json.dumps({"capacity": {
        "window_s": 300, "busy_pct": 77.0,
        "pools": {pool["id"]: {"points_per_s": 100.0}}}})})

    got = await svc.detail(session, pool["id"])
    assert got["points_per_s"] == 100.0
    assert got["budget_used_pct"] == 25.0
    assert got["busiest_member_pct"] == 77.0

    # A silent collector's last figure is history, not load.
    await session.execute(text("""
        UPDATE collector_instance SET last_heartbeat = now() - interval '10 minutes'
         WHERE id = :id
    """), {"id": cid})
    got = await svc.detail(session, pool["id"])
    assert got["points_per_s"] is None and got["busiest_member_pct"] is None


# --- shard map and drain (docs/26 Phase 5 frontend row, migration 0093) --------

async def _history(session, endpoint_id):
    return (await session.execute(text("""
        SELECT from_collector, to_collector, epoch, reason
          FROM endpoint_assignment_history
         WHERE endpoint_id = CAST(:e AS uuid) ORDER BY at, id
    """), {"e": endpoint_id})).mappings().all()


async def test_every_move_leaves_a_history_row_with_where_it_came_from(session):
    _, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    profile = await _poll_profile(session)
    eid = await _device_endpoint(session, room_id, profile, "10.52.21.10", "bacnet")

    await fleet_repo.write_assignment(session, {eid: ("col-a", "initial")})
    await fleet_repo.write_assignment(session, {eid: ("col-b", "drain")})
    await fleet_repo.write_assignment(session, {eid: (None, "pool_empty")})

    rows = [dict(r) for r in await _history(session, eid)]
    assert rows == [
        {"from_collector": None, "to_collector": "col-a", "epoch": 1, "reason": "initial"},
        {"from_collector": "col-a", "to_collector": "col-b", "epoch": 2, "reason": "drain"},
        {"from_collector": "col-b", "to_collector": None, "epoch": 3, "reason": "pool_empty"},
    ]
    newest_first = await fleet_repo.assignment_history(session, eid)
    assert [r["epoch"] for r in newest_first] == [3, 2, 1]


async def test_history_older_than_the_keep_window_is_pruned(session):
    _, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    profile = await _poll_profile(session)
    eid = await _device_endpoint(session, room_id, profile, "10.52.22.10", "bacnet")
    await fleet_repo.write_assignment(session, {eid: ("col-a", "initial")})
    await fleet_repo.write_assignment(session, {eid: ("col-b", "rebalance")})
    await session.execute(text("""
        UPDATE endpoint_assignment_history
           SET at = now() - make_interval(days => :d)
         WHERE endpoint_id = CAST(:e AS uuid) AND epoch = 1
    """), {"e": eid, "d": fleet_repo.HISTORY_KEEP_DAYS + 1})
    assert await fleet_repo.prune_assignment_history(session) >= 1
    assert [r["epoch"] for r in await _history(session, eid)] == [2]


async def test_a_drain_moves_the_pools_work_to_the_other_member_and_records_why(session):
    """End to end on the real schema: preview, mark draining, run the real
    assigner - the moved endpoints now belong to the other member, and every
    move is in the history as a drain."""
    from app.services import assigner, shard_map

    dc_id, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[], min_members=1,
                                              bbmd_settings={}))
    await _discovery_range(session, dc_id, "10.52.23.0/24", purpose="bms")
    profile = await _poll_profile(session)
    eids = [await _device_endpoint(session, room_id, profile, f"10.52.23.{i}", "bacnet")
            for i in range(10, 30)]
    a, b = f"col-{_tag()}", f"col-{_tag()}"
    await _collector(session, a, pool["id"])
    await _collector(session, b, pool["id"])
    await assigner.run(session)

    preview = await shard_map.drain_preview(session, a)
    assert preview["owned"] > 0, "HRW over 20 endpoints and 2 members gives each some"
    assert preview["destinations"] == {b: preview["moving"]}
    assert preview["can_drain"] is True

    await fleet_repo.set_state(session, a, "draining", "test")
    await assigner.run(session)

    recorded = {r["endpoint_id"]: r for r in (await session.execute(text("""
        SELECT endpoint_id::text, collector_id, reason FROM endpoint_assignment
         WHERE endpoint_id = ANY(CAST(:ids AS uuid[]))
    """), {"ids": eids})).mappings().all()}
    assert {r["collector_id"] for r in recorded.values()} == {b}
    moved = [e for e in eids if recorded[e]["reason"] == "drain"]
    assert len(moved) == preview["moving"]
    last = (await _history(session, moved[0]))[-1]
    assert (last["from_collector"], last["to_collector"], last["reason"]) == (a, b, "drain")
    assert await shard_map.remaining(session, a) == 0

    page = await shard_map.shard_map(session, pool_id=pool["id"], limit=500)
    assert page["total"] == 20
    assert all(r["owner"] == b and not r["record_disagrees"] for r in page["items"])


async def test_the_preview_refuses_to_strand_a_single_member_pool(session):
    from app.services import shard_map

    dc_id, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[], min_members=1,
                                              bbmd_settings={}))
    await _discovery_range(session, dc_id, "10.52.24.0/24", purpose="bms")
    profile = await _poll_profile(session)
    for i in range(10, 13):
        await _device_endpoint(session, room_id, profile, f"10.52.24.{i}", "bacnet")
    only = f"col-{_tag()}"
    await _collector(session, only, pool["id"])

    preview = await shard_map.drain_preview(session, only)
    assert preview["owned"] == 3 and preview["stranded"] == 3
    assert preview["can_drain"] is False


async def test_the_serving_path_honours_the_assigners_damping(session):
    """A second member joins an 8-endpoint pool: HRW would hand it about
    half at once, but the imbalance is under the damping floor, so the
    assigner keeps the record on the first member - and build_assignment
    now serves the record, so the newcomer is given nothing yet."""
    from app.services import assigner
    from app.services import collector as fleet_svc

    dc_id, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[], min_members=1,
                                              bbmd_settings={}))
    await _discovery_range(session, dc_id, "10.52.25.0/24", purpose="bms")
    profile = await _poll_profile(session)
    eids = {await _device_endpoint(session, room_id, profile, f"10.52.25.{i}", "bacnet")
            for i in range(10, 18)}
    a1, a2 = f"col-{_tag()}", f"col-{_tag()}"
    await _collector(session, a1, pool["id"])
    await assigner.run(session)
    await _collector(session, a2, pool["id"])
    await assigner.run(session)

    served_a1 = {e.id for e in (await fleet_svc.build_assignment(session, a1)).endpoints}
    served_a2 = {e.id for e in (await fleet_svc.build_assignment(session, a2)).endpoints}
    assert eids <= served_a1
    assert not (eids & served_a2), "the live plan alone would have given a2 some"


async def test_a_just_moved_endpoint_is_not_called_silent_before_its_new_owner_polls(session):
    """Found in the live HA failover: the new owner's liveness probe marks the
    endpoint ONLINE within 30 s, its first data poll lands later in the
    interval, and the last sample is the dead collector's, minutes old. The
    endpoint was called "answers but delivers no telemetry" for a minute."""
    from app.alarms import staleness

    _, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    profile = await _poll_profile(session)  # 30 s interval -> 300 s grace floor
    eid = await _device_endpoint(session, room_id, profile, "10.52.26.10", "snmp")
    await session.execute(text("""
        INSERT INTO endpoint_state (endpoint_id, status, last_success,
                                    last_telemetry_at, updated_at)
        VALUES (CAST(:e AS uuid), 'ONLINE', now(), now() - interval '8 minutes', now())
    """), {"e": eid})

    silent = {r["endpoint_id"] for r in await staleness.find_silent(session)}
    assert eid in silent, "8 min of silence under one owner IS silent"

    await fleet_repo.write_assignment(session, {eid: ("col-new", "failover")})
    silent = {r["endpoint_id"] for r in await staleness.find_silent(session)}
    assert eid not in silent, "its new owner has had it for seconds"


async def test_rebalance_now_moves_what_damping_held_and_only_in_its_pool(session):
    """docs/26 Phase 5's "rebalance now": a second member joins an 8-endpoint
    pool, damping keeps everything on the first, the preview shows the move,
    and the forced pass makes exactly that move - nowhere else."""
    from app.services import assigner, shard_map

    dc_id, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[], min_members=1,
                                              bbmd_settings={}))
    await _discovery_range(session, dc_id, "10.52.27.0/24", purpose="bms")
    profile = await _poll_profile(session)
    for i in range(10, 18):
        await _device_endpoint(session, room_id, profile, f"10.52.27.{i}", "bacnet")
    a1, a2 = f"col-{_tag()}", f"col-{_tag()}"
    await _collector(session, a1, pool["id"])
    await assigner.run(session)
    await _collector(session, a2, pool["id"])
    await assigner.run(session)

    preview = await shard_map.rebalance_preview(session, pool["id"])
    assert preview["endpoints"] == 8 and preview["before"] == {a1: 8}
    assert preview["moving"] > 0 and preview["automatic"] == 0
    assert {m["from"] for m in preview["moves"]} == {a1}
    assert {m["to"] for m in preview["moves"]} == {a2}

    others_before = await fleet_repo.current_assignment(session)
    result = await assigner.run(session, force_pool=pool["id"])
    assert result.ran and result.moved == preview["moving"]
    after = await shard_map.rebalance_preview(session, pool["id"])
    assert after["balanced"] and after["before"] == preview["after"]
    others_after = await fleet_repo.current_assignment(session)
    pool_eids = {eid for eid, owner in others_after.items() if owner in (a1, a2)}
    assert {k: v for k, v in others_before.items() if k not in pool_eids} == \
           {k: v for k, v in others_after.items() if k not in pool_eids}


# --- docs/26 Phase 7: commands and rollouts -------------------------------------

async def _release(session, version):
    from app.repositories import commands as cmd_repo
    await cmd_repo.add_release(session, {
        "version": version, "sha256": "ab" * 32, "signature": "c2ln", "key_id": "k1",
        "size_bytes": 1, "path": f"{version}/collector", "notes": None, "actor": "test"})


async def test_a_command_is_delivered_once_and_finished_once(session):
    from app.repositories import commands as cmd_repo

    cid = f"col-{_tag()}"
    await _collector(session, cid, None)
    await _release(session, f"9.9.{_tag()[:4]}")
    command = await cmd_repo.create(session, cid, "upgrade", {"version": "x"}, "test")
    first = await cmd_repo.claim_pending(session, cid)
    assert [c["id"] for c in first] == [command]
    assert await cmd_repo.claim_pending(session, cid) == [], "delivered, not re-delivered"
    assert await cmd_repo.finish(session, command, cid, "succeeded", {"detail": "ok"})
    assert not await cmd_repo.finish(session, command, cid, "failed", {}), "only once"
    assert not await cmd_repo.finish(session, command, "someone-else", "failed", {})


async def test_the_moves_token_changes_when_an_owner_changes(session):
    from app.repositories import commands as cmd_repo

    _, room_id = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    profile = await _poll_profile(session)
    eid = await _device_endpoint(session, room_id, profile, "10.52.28.10", "bacnet")
    before = await cmd_repo.moves_token(session)
    await session.execute(text("SELECT pg_sleep(0.01)"))
    await fleet_repo.write_assignment(session, {eid: ("col-x", "initial")})
    assert await cmd_repo.moves_token(session) != before


async def _state(session, cid):
    return (await session.execute(text(
        "SELECT state, state_changed_by FROM collector_instance WHERE id = :id"),
        {"id": cid})).mappings().one()


async def _age_state(session, cid):
    """Past the hand-off window, as if the drain had happened two minutes ago."""
    await session.execute(text("""
        UPDATE collector_instance SET state_changed_at = now() - interval '2 minutes'
         WHERE id = :id"""), {"id": cid})


async def _confirm(session, command_id, cid, version, settled):
    from app.repositories import commands as cmd_repo
    await cmd_repo.finish(session, command_id, cid, "succeeded", {"detail": "ok"})
    await session.execute(text("UPDATE collector_instance SET version = :v WHERE id = :id"),
                          {"v": version, "id": cid})
    if settled:
        await session.execute(text("""
            UPDATE collector_command SET finished_at = now() - interval '5 minutes'
             WHERE id = CAST(:id AS uuid)"""), {"id": command_id})


async def test_a_rollout_drains_upgrades_and_returns_one_member_at_a_time(session):
    """tick() end to end on the real schema: the first member is drained to
    its partner and commanded only after the hand-off window; returned once
    it confirms; the second waits for the first to settle; then done."""
    from app.repositories import commands as cmd_repo
    from app.services import rollout

    dc_id, _ = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[], min_members=1, bbmd_settings={}))
    a, b = sorted([f"col-{_tag()}", f"col-{_tag()}"])
    for cid in (a, b):
        await _collector(session, cid, pool["id"])
        await session.execute(text("""
            UPDATE collector_instance
               SET version = '1.0', healthy_since = now() - interval '2 hours'
             WHERE id = :id"""), {"id": cid})
    version = f"2.0.{_tag()[:4]}"
    await _release(session, version)
    rid = await cmd_repo.create_rollout(session, version, [pool["id"]], "test")
    tag = f"rollout:{rid}"

    assert await rollout.tick(session) == 0, "drained first, not commanded"
    assert dict(await _state(session, a)) == {"state": "draining", "state_changed_by": tag}
    assert (await _state(session, b))["state"] == "active"
    assert await rollout.tick(session) == 0, "inside the hand-off window"
    await _age_state(session, a)
    assert await rollout.tick(session) == 1
    cmds = await cmd_repo.rollout_commands(session, rid)
    assert [c["collector_id"] for c in cmds] == [a]
    assert await rollout.tick(session) == 0, "a is still upgrading"

    await _confirm(session, cmds[0]["id"], a, version, settled=False)
    await rollout.tick(session)
    assert (await _state(session, a))["state"] == "active", "returned once confirmed"
    await rollout.tick(session)
    assert (await _state(session, b))["state"] == "active", "a has not settled yet"

    await _confirm(session, cmds[0]["id"], a, version, settled=True)
    await rollout.tick(session)
    assert (await _state(session, b))["state"] == "draining"
    await _age_state(session, b)
    assert await rollout.tick(session) == 1
    second = [c for c in await cmd_repo.rollout_commands(session, rid) if c["collector_id"] == b]
    await _confirm(session, second[0]["id"], b, version, settled=True)
    await rollout.tick(session)
    assert (await _state(session, b))["state"] == "active"
    await rollout.tick(session)
    r = next(r for r in await cmd_repo.rollouts(session) if r["id"] == rid)
    assert r["state"] == "succeeded" and version in r["detail"]


async def test_a_failed_upgrade_returns_the_drained_member_once_it_heartbeats(session):
    """The rollout fails; the member it drained is not left out of service -
    but only once it is alive again on the build it rolled back to."""
    from app.repositories import commands as cmd_repo
    from app.services import rollout

    dc_id, _ = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[], min_members=1, bbmd_settings={}))
    a, b = sorted([f"col-{_tag()}", f"col-{_tag()}"])
    for cid in (a, b):
        await _collector(session, cid, pool["id"])
    version = f"4.0.{_tag()[:4]}"
    await _release(session, version)
    rid = await cmd_repo.create_rollout(session, version, [pool["id"]], "test")
    await rollout.tick(session)
    await _age_state(session, a)
    await rollout.tick(session)
    c = (await cmd_repo.rollout_commands(session, rid))[0]
    await cmd_repo.finish(session, c["id"], a, "failed", {"detail": "rolled back"})
    # Still crash-looping: its heartbeat is stale, so it stays drained.
    await session.execute(text("""
        UPDATE collector_instance SET last_heartbeat = now() - interval '5 minutes'
         WHERE id = :id"""), {"id": a})
    await rollout.tick(session)
    r = next(r for r in await cmd_repo.rollouts(session) if r["id"] == rid)
    assert r["state"] == "failed"
    assert (await _state(session, a))["state"] == "draining"
    await session.execute(text(
        "UPDATE collector_instance SET last_heartbeat = now() WHERE id = :id"),
                          {"id": a})
    await rollout.tick(session)
    assert (await _state(session, a))["state"] == "active"


async def test_a_failed_member_fails_the_rollout(session):
    from app.repositories import commands as cmd_repo
    from app.services import rollout

    dc_id, _ = await _datacenter_and_room(session, f"P{_tag()[:6]}")
    pool = await svc.create(session, _payload(dc_id, cidrs=[], min_members=1, bbmd_settings={}))
    cid = f"col-{_tag()}"
    await _collector(session, cid, pool["id"])
    version = f"3.0.{_tag()[:4]}"
    await _release(session, version)
    rid = await cmd_repo.create_rollout(session, version, [pool["id"]], "test")
    await rollout.tick(session)
    c = (await cmd_repo.rollout_commands(session, rid))[0]
    await cmd_repo.finish(session, c["id"], cid, "failed", {"detail": "rolled back"})
    await rollout.tick(session)
    r = next(r for r in await cmd_repo.rollouts(session) if r["id"] == rid)
    assert r["state"] == "failed" and "rolled back" in r["detail"]
