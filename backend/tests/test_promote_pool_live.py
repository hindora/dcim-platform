"""Promote's `pool` credential mode against the real schema: an endpoint
inherits its pool's SNMP default only where the address resolves into the
pool whose credential the sweep proved."""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import discovery_endpoints as de

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


async def _site(session, with_default: bool = True) -> dict:
    tag = uuid.uuid4().hex[:6]
    dc = await session.scalar(text("""
        INSERT INTO datacenter (code, name) VALUES (:c, :n) RETURNING id::text
    """), {"c": f"T{tag}", "n": f"T{tag}"})
    room = await session.scalar(text("""
        INSERT INTO room (datacenter_id, name) VALUES (CAST(:dc AS uuid), 'main')
        RETURNING id::text
    """), {"dc": dc})
    await session.execute(text("""
        INSERT INTO discovery_range (cidr, name, datacenter_id, purpose)
        VALUES ('10.52.240.0/24', :n, CAST(:dc AS uuid), 'bms')
    """), {"n": f"r-{tag}", "dc": dc})
    pool = await session.scalar(text("""
        INSERT INTO collector_pool (name, datacenter_id, plane, cidrs)
        VALUES (:n, CAST(:dc AS uuid), 'bms', '{}') RETURNING id::text
    """), {"n": f"pool-{tag}", "dc": dc})
    if with_default:
        cred = await session.scalar(text("""
            INSERT INTO credential (name, protocol, kind, secret_enc)
            VALUES (:n, 'snmp', 'snmp_v3', 'x') RETURNING id::text
        """), {"n": f"v3-{tag}"})
        await session.execute(text("""
            INSERT INTO collector_pool_credential (pool_id, protocol, credential_id)
            VALUES (CAST(:p AS uuid), 'snmp', CAST(:c AS uuid))
        """), {"p": pool, "c": cred})
    device = await session.scalar(text("""
        INSERT INTO device (name, device_type, lifecycle, room_id)
        VALUES (:n, 'ups', 'in_service', CAST(:r AS uuid)) RETURNING id::text
    """), {"n": f"ups-{tag}", "r": room})
    return {"pool": pool, "device": device}


async def test_pool_mode_inherits_where_the_sweep_proved_it(session):
    s = await _site(session)
    got = await de._credential(session, "snmp", "10.52.240.25",
                               {"mode": "pool", "pool_id": s["pool"]}, s["device"])
    assert got is None, "no credential of its own: it inherits the pool's"


async def test_pool_mode_refuses_another_pools_evidence(session):
    s = await _site(session)
    with pytest.raises(de.EndpointPlanError, match="different pool"):
        await de._credential(session, "snmp", "10.52.240.25",
                             {"mode": "pool", "pool_id": str(uuid.uuid4())}, s["device"])


async def test_pool_mode_refuses_an_address_in_no_pool(session):
    s = await _site(session)
    with pytest.raises(de.EndpointPlanError, match="no pool's range"):
        await de._credential(session, "snmp", "10.99.0.1",
                             {"mode": "pool", "pool_id": s["pool"]}, s["device"])


async def test_pool_mode_refuses_a_pool_with_nothing_to_inherit(session):
    s = await _site(session, with_default=False)
    with pytest.raises(de.EndpointPlanError, match="no SNMP default"):
        await de._credential(session, "snmp", "10.52.240.25",
                             {"mode": "pool", "pool_id": s["pool"]}, s["device"])
