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
