"""collector_preflight_result against a real database (docs/26 Phase 8).

Skipped unless DCIM_TEST_DATABASE_URL is set. Every test opens a
transaction and rolls it back.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.repositories import preflight as repo

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


def _cid() -> str:
    return f"col-{uuid.uuid4().hex[:8]}"


async def test_a_recorded_run_comes_back_as_latest(session):
    cid = _cid()
    checks = [{"check": "ntp_offset", "status": "ok", "value": 3.2}]
    await repo.record(session, cid, True, checks)

    row = await repo.latest(session, cid)
    assert row is not None
    assert row["collector_id"] == cid
    assert row["passed"] is True
    assert row["checks"] == checks


async def test_a_collector_with_no_runs_has_no_latest(session):
    assert await repo.latest(session, _cid()) is None


async def test_history_is_newest_first(session):
    cid = _cid()
    await repo.record(session, cid, False, [{"check": "a", "status": "fail"}])
    await repo.record(session, cid, True, [{"check": "a", "status": "ok"}])

    rows = await repo.history(session, cid)
    assert len(rows) == 2
    assert rows[0]["passed"] is True
    assert rows[1]["passed"] is False


async def test_history_is_scoped_per_collector(session):
    a, b = _cid(), _cid()
    await repo.record(session, a, True, [])
    await repo.record(session, b, True, [])

    assert len(await repo.history(session, a)) == 1
    assert len(await repo.history(session, b)) == 1


async def test_a_result_can_name_a_collector_with_no_row_yet(session):
    """Preflight can run before POST /collectors ever created a row -
    an operator racing a scripted install against the create-collector API
    call - and the result must still be kept, not silently discarded."""
    cid = _cid()
    await repo.record(session, cid, True, [{"check": "core_tls", "status": "ok"}])
    assert (await repo.latest(session, cid))["collector_id"] == cid
