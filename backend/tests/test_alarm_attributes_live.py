"""raise_alarm(..., attributes=) on a real database (docs/29 D13, docs/30 1).

The anomaly sweep links a derived alarm to its finding through the existing
alarm.attributes jsonb. Stored on create, shallow-merged on conflict, and an
omitted argument leaves the column untouched - which is what keeps every L1
caller, none of which passes it, exactly as it was.

Skipped unless DCIM_TEST_DATABASE_URL is set. The transaction is rolled back.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.repositories import alarms as repo

DB_URL = os.getenv("DCIM_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run"),
    pytest.mark.asyncio,
]

AT = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


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


async def _device(session) -> str:
    return await session.scalar(text("""
        INSERT INTO device (name, device_type, lifecycle)
        VALUES (:n, 'crah', 'in_service') RETURNING id::text
    """), {"n": f"crah-{uuid.uuid4().hex[:8]}"})


async def _raise(session, device_id, **kw):
    return await repo.raise_alarm(
        session, device_id=device_id, alarm_type="crah_supply_over_setpoint",
        instance="", severity="MINOR", message="supply over setpoint",
        source="analytics", observed_at=AT, metric_key="supply_air_temp",
        value=27.0, threshold=24.0, **kw)


async def _attrs(session, alarm_id):
    return await session.scalar(text(
        "SELECT attributes FROM alarm WHERE id = CAST(:i AS uuid)"), {"i": alarm_id})


async def test_attributes_are_stored_on_create(session):
    dev = await _device(session)
    link = {"anomaly": {"finding_id": "f1", "detector_id": "CR-1", "detector_version": 1,
                        "expected": 24.0, "observed": 27.0}}
    row = await _raise(session, dev, attributes=link)
    assert row["change"] == "created"
    assert "attributes" not in row  # the websocket shape is unchanged
    assert await _attrs(session, row["id"]) == link


async def test_attributes_merge_on_conflict_and_omitted_is_untouched(session):
    dev = await _device(session)
    first = await _raise(session, dev, attributes={"anomaly": {"finding_id": "f1"}})

    again = await _raise(session, dev, attributes={"jira": {"key": "OPS-1"}})
    assert again["id"] == first["id"]
    assert await _attrs(session, first["id"]) == {"anomaly": {"finding_id": "f1"},
                                                  "jira": {"key": "OPS-1"}}

    await _raise(session, dev)  # omitted
    assert await _attrs(session, first["id"]) == {"anomaly": {"finding_id": "f1"},
                                                  "jira": {"key": "OPS-1"}}

    # Shallow: a top-level key is replaced whole.
    await _raise(session, dev, attributes={"anomaly": {"finding_id": "f2"}})
    assert await _attrs(session, first["id"]) == {"anomaly": {"finding_id": "f2"},
                                                  "jira": {"key": "OPS-1"}}


async def test_an_alarm_raised_without_attributes_gets_the_default(session):
    dev = await _device(session)
    row = await _raise(session, dev)
    assert await _attrs(session, row["id"]) == {}
    detection = await session.scalar(text(
        "SELECT detection FROM alarm WHERE id = CAST(:i AS uuid)"), {"i": row["id"]})
    assert detection == "derived"
