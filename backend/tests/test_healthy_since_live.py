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
from datetime import UTC

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.repositories import collector as fleet_repo

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
            healthy_since = __HEALTHY_SINCE__
    """.replace("__HEALTHY_SINCE__", fleet_repo.HEALTHY_SINCE_ON_HEARTBEAT)),
        {"id": collector_id})


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


async def test_a_record_created_ahead_of_its_install_starts_a_streak(session):
    """Found in the live HA test: POST /collectors writes a creation-time
    last_heartbeat, so the first real heartbeat landed inside the 60 s
    window and the streak stayed NULL for good."""
    cid = f"col-{uuid.uuid4().hex[:8]}"
    await fleet_repo.create_collector(session, cid, None, "test")
    assert await _healthy_since(session, cid) is None
    await _heartbeat(session, cid)
    assert await _healthy_since(session, cid) is not None


async def test_the_http_fallback_heartbeat_keeps_started_at_and_the_streak(session):
    """A gateway-transport collector heartbeats over HTTP. That path never
    set started_at on an existing record - so a collector created ahead of
    its install never counted as having run, and was never given work - and
    never kept a streak at all."""
    import json
    from datetime import datetime

    cid = f"col-{uuid.uuid4().hex[:8]}"
    await fleet_repo.create_collector(session, cid, None, "test")
    hb = {"id": cid, "version": "t", "hostname": "h",
          "started_at": datetime.now(UTC), "endpoints_owned": 0,
          "endpoints_online": 0, "stats": json.dumps({})}
    await fleet_repo.upsert_heartbeat(session, hb)
    row = (await session.execute(text(
        "SELECT started_at, healthy_since FROM collector_instance WHERE id = :id"),
        {"id": cid})).mappings().one()
    assert row["started_at"] is not None and row["healthy_since"] is not None
    first = row["healthy_since"]
    await fleet_repo.upsert_heartbeat(session, hb)
    assert await _healthy_since(session, cid) == first, "a prompt heartbeat continues it"


async def test_a_heartbeat_from_an_older_process_marks_a_duplicate(session):
    """Two live processes under one identity alternate heartbeats; the older
    one's start time going BACKWARDS is impossible for a single process."""
    import json
    from datetime import datetime, timedelta

    from app.alarms import platform_monitor as mon

    cid = f"col-{uuid.uuid4().hex[:8]}"
    await fleet_repo.create_collector(session, cid, None, "test")
    now = datetime.now(UTC)
    hb = {"id": cid, "version": "t", "hostname": "h", "endpoints_owned": 0,
          "endpoints_online": 0, "stats": json.dumps({})}
    await fleet_repo.upsert_heartbeat(session, {**hb, "started_at": now - timedelta(minutes=30)})
    await fleet_repo.upsert_heartbeat(session, {**hb, "started_at": now})  # a restart: fine
    seen = await session.scalar(text(
        "SELECT stats->>'duplicate_seen_at' FROM collector_instance WHERE id = :id"), {"id": cid})
    assert seen is None, "a start time moving forward is a restart, not a duplicate"

    await fleet_repo.upsert_heartbeat(session, {**hb, "started_at": now - timedelta(minutes=30)})
    seen = await session.scalar(text(
        "SELECT stats->>'duplicate_seen_at' FROM collector_instance WHERE id = :id"), {"id": cid})
    assert seen is not None
    cols = {c.collector_id: c for c in await mon._collectors(session)}
    assert cols[cid].duplicate_age_s is not None and cols[cid].duplicate_age_s < 60


async def test_a_dead_process_heartbeat_replayed_late_is_not_a_duplicate(session):
    """After a stack restart the stream still holds heartbeats the dead
    processes sent, and a backlog delivers them after the new processes'
    first ones. Sent before the current process started: a replay, not a
    second process. Sent after it: two processes really are alive."""
    import json
    from datetime import datetime, timedelta

    cid = f"col-{uuid.uuid4().hex[:8]}"
    await fleet_repo.create_collector(session, cid, None, "test")
    now = datetime.now(UTC)
    old_start = now - timedelta(hours=5)
    new_start = now - timedelta(minutes=3)
    hb = {"id": cid, "version": "t", "hostname": "h", "endpoints_owned": 0,
          "endpoints_online": 0, "stats": json.dumps({})}

    async def seen():
        return await session.scalar(text(
            "SELECT stats->>'duplicate_seen_at' FROM collector_instance WHERE id = :id"),
            {"id": cid})

    await fleet_repo.upsert_heartbeat(
        session, {**hb, "started_at": new_start, "sent_at": now - timedelta(minutes=2)})
    # The dead process's last heartbeat, sent ten minutes before the restart.
    await fleet_repo.upsert_heartbeat(
        session, {**hb, "started_at": old_start, "sent_at": new_start - timedelta(minutes=10)})
    assert await seen() is None, "a replay from a process that died is not a duplicate"

    await fleet_repo.upsert_heartbeat(
        session, {**hb, "started_at": new_start, "sent_at": now - timedelta(seconds=30)})
    # The older process is still talking after the new one started.
    await fleet_repo.upsert_heartbeat(
        session, {**hb, "started_at": old_start, "sent_at": now})
    assert await seen() is not None, "two live processes are a duplicate"


async def test_a_heartbeat_on_the_bus_keeps_a_collector_alive_before_it_is_processed(session):
    """The worker stalled; the collector did not. Its newest heartbeat on the
    stream moves last_heartbeat forward, never back."""
    import msgpack

    from app.alarms import collector_visibility as cv

    cid = f"col-{uuid.uuid4().hex[:8]}"
    await fleet_repo.create_collector(session, cid, None, "test")
    await session.execute(text(
        "UPDATE collector_instance SET last_heartbeat = now() - interval '5 minutes'"
        " WHERE id = :id"),
        {"id": cid})

    class _Bus:
        def __init__(self, ms):
            self.ms = ms

        async def xrevrange(self, stream, count):
            return [(f"{self.ms}-0".encode(), {b"p": msgpack.packb({"collector_id": cid})})]

    async def age():
        return await session.scalar(text(
            "SELECT extract(epoch FROM now() - last_heartbeat)"
            " FROM collector_instance WHERE id = :id"),
            {"id": cid})

    now_ms = int(await session.scalar(text("SELECT extract(epoch FROM now()) * 1000")))
    await cv.seen_on_bus(session, _Bus(now_ms - 5_000), "collectorhb.v1")
    assert await age() < 30, "a heartbeat 5 s ago on the bus is a live collector"
    await cv.seen_on_bus(session, _Bus(now_ms - 600_000), "collectorhb.v1")
    assert await age() < 30, "an older bus entry never moves it back"
