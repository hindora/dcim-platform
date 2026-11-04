"""How a device is cooled, against the real fleet. Read-only.

Skipped unless DCIM_TEST_DATABASE_URL points at a database with an imported
fleet. Nothing is written, so it is safe beside a running campaign.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.alarms import cooling_correlation as cc
from app.services import cooling_trace, racks

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
        cc.reset_graph()
        yield s
        await s.rollback()
    await engine.dispose()


async def _server(s, liquid: bool) -> str:
    cond = "EXISTS" if liquid else "NOT EXISTS"
    return await s.scalar(text(f"""
        SELECT d.id::text FROM device d
         WHERE d.device_type = 'server' AND d.room_id IS NOT NULL AND {cond} (
               SELECT 1 FROM connection c JOIN device u
                 ON u.id IN (c.a_device_id, c.b_device_id) AND u.device_type = 'cdu'
                WHERE c.layer = 'cooling' AND (c.a_device_id = d.id OR c.b_device_id = d.id))
         ORDER BY d.name LIMIT 1"""))


async def test_a_cold_plate_server_has_both_halves_of_its_cooling(session):
    out = await cooling_trace.device_cooling(session, await _server(session, True))
    assert out["method"] == "liquid" and out["method_basis"] == "drawn"
    assert out["liquid"]["cdus"] and out["liquid"]["share"] == pytest.approx(0.7)
    assert out["air"]["basis"] == "room" and out["air"]["units"]
    assert out["air"]["share"] == pytest.approx(0.3)
    assert any(m["device_type"] == "chiller" for m in out["plant"]["machines"])


async def test_an_air_server_has_only_the_room(session):
    out = await cooling_trace.device_cooling(session, await _server(session, False))
    assert out["method"] == "air" and out["liquid"] is None and out["air"]["share"] == 1.0


async def test_a_mixed_rack_counts_both(session):
    srv = await _server(session, True)
    rack = await session.scalar(
        text("SELECT rack_id::text FROM device WHERE id = CAST(:i AS uuid)"), {"i": srv})
    d = await racks.rack_detail(session, rack)
    assert d["space"]["cooling"]["liquid"] >= 1
    row = next(r for r in d["devices"] if r["id"] == srv)
    assert row["cooling_method"] == "liquid"
