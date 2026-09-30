"""healthy_since tracking against a real database (docs/26 Phase 6).

Mirrors the exact UPSERT app/ingest/worker.py's _handle_heartbeat runs -
duplicated here rather than calling the private method directly, since
IngestWorker's constructor pulls in Redis and the rest of the pipeline for
what is otherwise a single SQL statement. If this drifts from the real
statement, that is a real risk worth flagging; the two are kept
side-by-side in these two files for exactly that reason.

Skipped unless DCIM_TEST_DATABASE_URL is set. Every test opens a transaction
and rolls it back.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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


async def _heartbeat(session, collector_id: str) -> None:
    # clock_timestamp(), not now(): now() is fixed for the whole test
    # transaction, which would make two heartbeats in the same test see an
    # identical, frozen "now" - see the matching comment in
    # app/ingest/worker.py's _handle_heartbeat, which this mirrors.
    await session.execute(text("""
        INSERT INTO collector_instance
            (id, version, hostname, started_at, last_heartbeat,
             endpoints_owned, endpoints_online, status, stats, state, healthy_since)
        VALUES (:id, 'test', 'test-host', clock_timestamp(), clock_timestamp(),
                0, 0, 'HEALTHY', '{}'::jsonb, 'active', clock_timestamp())
        ON CONFLICT (id) DO UPDATE SET
            last_heartbeat = clock_timestamp(),
            healthy_since = CASE
                WHEN collector_instance.last_heartbeat IS NULL
                     OR clock_timestamp() - collector_instance.last_heartbeat
                        > interval '60 seconds'
                  THEN clock_timestamp()
                ELSE collector_instance.healthy_since
            END
    """), {"id": collector_id})


async def _healthy_since(session, collector_id: str):
    return await session.scalar(text(
        "SELECT healthy_since FROM collector_instance WHERE id = :id"),
        {"id": collector_id})


async def test_a_first_heartbeat_starts_the_streak(session):
    cid = f"col-{uuid.uuid4().hex[:8]}"
    await _heartbeat(session, cid)
    since = await _healthy_since(session, cid)
    assert since is not None


async def test_a_prompt_second_heartbeat_does_not_reset_the_streak(session):
    cid = f"col-{uuid.uuid4().hex[:8]}"
    await _heartbeat(session, cid)
    first = await _healthy_since(session, cid)
    await _heartbeat(session, cid)
    second = await _healthy_since(session, cid)
    assert first == second


async def test_a_gap_past_the_stale_cutoff_resets_the_streak(session):
    cid = f"col-{uuid.uuid4().hex[:8]}"
    await _heartbeat(session, cid)
    first = await _healthy_since(session, cid)
    # Simulate the stale gap directly - waiting 60 real seconds in a test
    # would be its own kind of bug.
    await session.execute(text("""
        UPDATE collector_instance
           SET last_heartbeat = now() - interval '90 seconds'
         WHERE id = :id
    """), {"id": cid})
    await _heartbeat(session, cid)
    second = await _healthy_since(session, cid)
    assert second != first
    assert second is not None


async def test_a_gap_under_the_stale_cutoff_does_not_reset(session):
    cid = f"col-{uuid.uuid4().hex[:8]}"
    await _heartbeat(session, cid)
    first = await _healthy_since(session, cid)
    await session.execute(text("""
        UPDATE collector_instance
           SET last_heartbeat = now() - interval '30 seconds'
         WHERE id = :id
    """), {"id": cid})
    await _heartbeat(session, cid)
    second = await _healthy_since(session, cid)
    assert second == first
