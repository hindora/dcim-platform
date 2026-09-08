"""Booleans are written on change plus a heartbeat, decided atomically in Redis.

Against a real Redis for the same reason test_baseline_exchange is: the claim
under test is that two workers sharing a consumer group cannot both write the
same unchanged value, and a fake that serialises everything would prove
nothing about that.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.ingest import changelog
from app.ingest.writer import BoolRow

REDIS_URL = os.getenv("DCIM_TEST_REDIS_URL")

pytestmark = [
    pytest.mark.skipif(not REDIS_URL, reason="set DCIM_TEST_REDIS_URL to run"),
    pytest.mark.asyncio,
]

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def redis():
    r = Redis.from_url(REDIS_URL)
    yield r
    await r.aclose()


@pytest.fixture
def device():
    return f"test-{uuid.uuid4().hex}"


def row(device: str, value: bool, at: datetime, *, quality: str = "good",
        instance: str = "") -> BoolRow:
    return BoolRow(ts=at, device_id=device, metric_id=7, instance=instance,
                   value=value, quality=quality)


async def test_the_first_sight_is_written(redis, device):
    got = await changelog.admit(redis, [row(device, True, T0)])
    assert len(got) == 1


async def test_an_unchanged_value_inside_the_heartbeat_is_not(redis, device):
    await changelog.admit(redis, [row(device, True, T0)])
    got = await changelog.admit(redis, [row(device, True, T0 + timedelta(seconds=78))])
    assert got == []


async def test_a_change_is_always_written(redis, device):
    await changelog.admit(redis, [row(device, True, T0)])
    got = await changelog.admit(redis, [row(device, False, T0 + timedelta(seconds=78))])
    assert len(got) == 1 and got[0].value is False


async def test_quality_is_part_of_the_state(redis, device):
    await changelog.admit(redis, [row(device, True, T0)])
    got = await changelog.admit(
        redis, [row(device, True, T0 + timedelta(seconds=78), quality="suspect")])
    assert len(got) == 1, "good -> suspect on the same value is a change"


async def test_the_heartbeat_rewrites_an_unchanged_value(redis, device):
    await changelog.admit(redis, [row(device, True, T0)])
    just_under = T0 + timedelta(seconds=changelog.BOOL_HEARTBEAT_S - 1)
    at = T0 + timedelta(seconds=changelog.BOOL_HEARTBEAT_S)
    assert await changelog.admit(redis, [row(device, True, just_under)]) == []
    assert len(await changelog.admit(redis, [row(device, True, at)])) == 1


async def test_the_heartbeat_restarts_from_the_last_row_written(redis, device):
    """A skipped sample does not push the heartbeat out: the clock runs from
    the last ROW, or a series polled every 78 s would never heartbeat."""
    await changelog.admit(redis, [row(device, True, T0)])
    for i in range(1, 12):
        await changelog.admit(redis, [row(device, True, T0 + timedelta(seconds=78 * i))])
    at = T0 + timedelta(seconds=changelog.BOOL_HEARTBEAT_S + 1)
    assert len(await changelog.admit(redis, [row(device, True, at)])) == 1


async def test_a_replayed_older_sample_is_dropped(redis, device):
    await changelog.admit(redis, [row(device, True, T0)])
    got = await changelog.admit(redis, [row(device, False, T0 - timedelta(seconds=78))])
    assert got == [], "a reclaimed batch from before the installed state is a replay"


async def test_instances_are_separate_series(redis, device):
    await changelog.admit(redis, [row(device, True, T0, instance="eth0")])
    got = await changelog.admit(redis, [row(device, True, T0, instance="eth1")])
    assert len(got) == 1


async def test_a_flip_and_back_inside_one_batch_keeps_both(redis, device):
    got = await changelog.admit(redis, [
        row(device, True, T0),
        row(device, False, T0 + timedelta(seconds=78)),
        row(device, True, T0 + timedelta(seconds=156)),
    ])
    assert [r.value for r in got] == [True, False, True]


async def test_two_workers_write_an_unchanged_value_once(redis, device):
    """Twelve workers each see the same unchanged reading at the same
    moment. Exactly one row may result."""
    at = T0 + timedelta(seconds=78)
    await changelog.admit(redis, [row(device, True, T0)])
    clients = [Redis.from_url(REDIS_URL) for _ in range(12)]
    try:
        results = await asyncio.gather(*[
            changelog.admit(c, [row(device, True, at + timedelta(
                seconds=changelog.BOOL_HEARTBEAT_S))]) for c in clients])
    finally:
        for c in clients:
            await c.aclose()
    assert sum(len(r) for r in results) == 1


async def test_the_last_known_window_holds_a_heartbeat():
    assert changelog.LAST_KNOWN_WINDOW_S >= 2 * changelog.BOOL_HEARTBEAT_S
    assert changelog.BOOL_STATE_TTL_S > changelog.LAST_KNOWN_WINDOW_S
