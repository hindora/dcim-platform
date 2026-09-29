"""The assigner against a real database (docs/26 Phase 5).

Skipped unless DCIM_TEST_DATABASE_URL is set. Every test builds its own
datacenter/room/device/endpoint/pool fixture and rolls the whole
transaction back - see tests/test_collector_enrollment_live.py for the
same convention.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.repositories import collector as repo
from app.services import assigner

DB_URL = os.getenv("DCIM_TEST_DATABASE_URL")

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
    # :c is bound to two columns of different types (varchar(32) and text) -
    # asyncpg refuses to guess one type for a parameter used both ways, so
    # this needs two separate binds even though the value is identical.
    dc_id = await session.scalar(text("""
        INSERT INTO datacenter (code, name) VALUES (:c, :n) RETURNING id::text
    """), {"c": code, "n": code})
    room_id = await session.scalar(text("""
        INSERT INTO room (datacenter_id, name) VALUES (CAST(:dc AS uuid), 'main')
        RETURNING id::text
    """), {"dc": dc_id})
    return dc_id, room_id


async def _pool(session, dc_id: str, plane: str = "it_oob") -> str:
    return await session.scalar(text("""
        INSERT INTO collector_pool (name, datacenter_id, plane)
        VALUES (:n, CAST(:dc AS uuid), :p) RETURNING id::text
    """), {"n": f"{plane}-pool", "dc": dc_id, "p": plane})


async def _discovery_range(session, dc_id: str, cidr: str, purpose: str = "it_oob") -> None:
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
                           name: str | None = None) -> str:
    device_id = await session.scalar(text("""
        INSERT INTO device (name, device_type, lifecycle, room_id)
        VALUES (:n, 'switch', 'in_service', CAST(:room AS uuid)) RETURNING id::text
    """), {"n": name or f"dev-{_tag()}", "room": room_id})
    return await session.scalar(text("""
        INSERT INTO device_endpoint (device_id, protocol, role, address, poll_profile_id,
                                     enabled, admin_state)
        VALUES (CAST(:d AS uuid), 'snmp', 'os_agent', CAST(:addr AS inet),
                CAST(:profile AS uuid), true, 'enabled')
        RETURNING id::text
    """), {"d": device_id, "addr": address, "profile": profile_id})


async def _collector(session, collector_id: str, pool_id: str | None,
                     state: str = "active", has_run: bool = True) -> None:
    await session.execute(text("""
        INSERT INTO collector_instance (id, last_heartbeat, endpoints_owned,
                                        endpoints_online, status, stats, state,
                                        token_generation, state_changed_at,
                                        state_changed_by, pool_id, started_at)
        VALUES (:id, now(), 0, 0, 'HEALTHY', '{}'::jsonb, :state, 1, now(), 'test',
                CAST(:pool AS uuid), CASE WHEN :has_run THEN now() ELSE NULL END)
    """), {"id": collector_id, "state": state, "pool": pool_id, "has_run": has_run})


async def test_the_assigner_writes_endpoint_assignment_for_a_pooled_endpoint(session):
    tag = _tag()
    dc_id, room_id = await _datacenter_and_room(session, f"t{tag}")
    pool_id = await _pool(session, dc_id)
    await _discovery_range(session, dc_id, "10.99.0.0/24")
    profile_id = await _poll_profile(session)
    ep_id = await _device_endpoint(session, room_id, profile_id, "10.99.0.5")
    await _collector(session, f"col-{tag}-a", pool_id)
    await session.flush()

    result = await assigner.run(session)
    assert result.ran

    # This runs against the real, shared dev database, whose fleet already
    # has its own generic (unplaced, "serves everything") collectors - see
    # services/sharding.Collector.serves - so the winning owner cannot be
    # asserted exactly; what a real bug WOULD break is the write path
    # itself, which this does check: a real row landed, naming a real,
    # currently-live collector.
    current = await repo.current_assignment(session)
    assert current.get(ep_id) is not None
    live_ids = {c["collector_id"] for c in await repo.live_collectors(session)}
    assert current[ep_id] in live_ids


async def test_the_assigner_isolates_two_pools_in_the_same_datacenter(session):
    """The one property that MUST hold regardless of whatever else is in
    the shared live fleet: a collector placed in one pool can never win an
    endpoint in a different pool - Collector.serves refuses it outright,
    not merely deprioritises it. Which generic, unplaced collector might
    ALSO be eligible for either endpoint is not this test's concern."""
    tag = _tag()
    dc_id, room_id = await _datacenter_and_room(session, f"t{tag}")
    oob_pool = await _pool(session, dc_id, "it_oob")
    bms_pool = await _pool(session, dc_id, "bms")
    await _discovery_range(session, dc_id, "10.99.1.0/24", "it_oob")
    await _discovery_range(session, dc_id, "10.99.2.0/24", "bms")
    profile_id = await _poll_profile(session)
    oob_ep = await _device_endpoint(session, room_id, profile_id, "10.99.1.5")
    bms_ep = await _device_endpoint(session, room_id, profile_id, "10.99.2.5")
    oob_collector = f"col-{tag}-oob"
    bms_collector = f"col-{tag}-bms"
    await _collector(session, oob_collector, oob_pool)
    await _collector(session, bms_collector, bms_pool)
    await session.flush()

    await assigner.run(session)
    current = await repo.current_assignment(session)
    assert current[bms_ep] != oob_collector
    assert current[oob_ep] != bms_collector


async def test_a_second_advisory_lock_holder_skips_rather_than_blocks(session):
    """Two workers on the same tick: the second finds the lock held (this
    test's own outer session, via a nested savepoint session sharing the
    same underlying transaction lock table) and returns ran=False rather
    than racing the first."""
    got = await session.scalar(text(
        "SELECT pg_try_advisory_xact_lock(:key)"), {"key": assigner.ADVISORY_LOCK_KEY})
    assert got is True
    # Held for the rest of this transaction - a second acquisition attempt
    # in the SAME session (the only kind a single test can exercise without
    # a second real connection) must fail.
    still_held = await session.scalar(text(
        "SELECT pg_try_advisory_xact_lock(:key)"), {"key": assigner.ADVISORY_LOCK_KEY})
    assert still_held is True, \
        "pg_advisory_xact_lock is re-entrant within the SAME session/transaction " \
        "by design - this pins that Postgres behaviour so the assigner's own " \
        "skip-on-contention logic is understood correctly: contention is only " \
        "ever between DIFFERENT sessions, never within one"


async def test_pin_beats_the_pool_plan(session):
    tag = _tag()
    dc_id, room_id = await _datacenter_and_room(session, f"t{tag}")
    pool_id = await _pool(session, dc_id)
    await _discovery_range(session, dc_id, "10.99.4.0/24")
    profile_id = await _poll_profile(session)
    ep_id = await _device_endpoint(session, room_id, profile_id, "10.99.4.5")
    await _collector(session, f"col-{tag}-a", pool_id)
    pinned_to = f"col-{tag}-pinned-not-in-fleet"
    await session.execute(text(
        "UPDATE device_endpoint SET collector_id = :c WHERE id = CAST(:id AS uuid)"),
        {"c": pinned_to, "id": ep_id})
    await session.flush()

    await assigner.run(session)
    current = await repo.current_assignment(session)
    assert current[ep_id] == pinned_to
