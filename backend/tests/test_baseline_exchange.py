"""Counter baselines under more than one worker.

A consumer group hands out ENTRIES, not series. One batch carries samples for
hundreds of devices, and consecutive batches for the same interface go to
whichever worker was free, so no partitioning scheme gives a series a permanent
owner - and even hashing devices to workers would not, because when a worker
dies its pending entries are reclaimed by another and during that handover the
same series really is touched by two.

So the shared state has to be safe on its own. It was not: read every baseline
with MGET, compute, run a database transaction, then SET - a read-modify-write
with the whole commit inside the window. Two workers both read t0, both
computed their delta from t0, and an interface's throughput came out silently
wrong rather than missing.

Run against a real Redis, because the thing under test is an atomicity claim
about Redis, and a fake that serialises everything would prove nothing.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.ingest import rates
from app.ingest.worker import IngestWorker

REDIS_URL = os.getenv("DCIM_TEST_REDIS_URL")

pytestmark = [
    pytest.mark.skipif(not REDIS_URL, reason="set DCIM_TEST_REDIS_URL to run"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def worker():
    """A worker with nothing but the Redis it needs for this one method."""
    w = IngestWorker.__new__(IngestWorker)
    w.redis = Redis.from_url(REDIS_URL)
    yield w
    await w.redis.aclose()


@pytest_asyncio.fixture
def key():
    return f"test:{uuid.uuid4().hex}|if_in_octets|eth0"


async def test_the_first_reading_has_no_previous(worker, key):
    got = await worker._exchange_baselines({key: "1000:1000000"})
    assert got == {}, "a series with no history must not invent one"


async def test_the_reading_left_behind_is_the_one_just_taken(worker, key):
    await worker._exchange_baselines({key: "1000:1000000"})
    got = await worker._exchange_baselines({key: "1500:2000000"})
    assert got[key] == rates.Baseline(value=1000, observed_at_us=1000000)


WORKERS = 12
READINGS = 12


def _exchange_in_own_loop(key: str, first: int) -> list[tuple[int, int]]:
    """One worker, its own event loop and its own Redis connection.

    asyncio.gather is not enough to test this: two coroutines on one loop take
    turns at await points and ran the whole read-then-write to completion one
    after the other, so the unfixed code passed. Separate threads with separate
    connections put the calls genuinely in flight together, which is the only
    way the window is observable.

    Returns (previous_ts, own_ts) so the caller can tell which exchanges
    actually advanced the baseline.
    """
    async def run() -> list[tuple[int, int]]:
        w = IngestWorker.__new__(IngestWorker)
        w.redis = Redis.from_url(REDIS_URL)
        seen: list[tuple[int, int]] = []
        try:
            for n in range(READINGS):
                ts = (first + n * WORKERS + 1) * 1_000_000
                got = await w._exchange_baselines({key: f"{ts}:{ts}"})
                if key in got:
                    seen.append((got[key].observed_at_us, ts))
        finally:
            await w.redis.aclose()
        return seen

    return asyncio.run(run())


async def test_no_two_advancing_workers_share_a_previous_reading(worker, key):
    """The bug, as a rule.

    Whoever advances the baseline must see what the previous one left, not what
    they both started from. Two callers handed the same previous reading
    measure their deltas from the same point: one interface's throughput is
    wrong, and nothing records which.

    Only the exchanges that ADVANCED the baseline are counted. A caller whose
    reading is older than what is already stored is refused, and several such
    callers legitimately see the same newer value - they are late, they install
    nothing, and `derive` discards their rate on the non-positive interval.
    Counting those as collisions is what made the first version of this test
    fail against correct code.
    """
    await worker._exchange_baselines({key: "1:1"})

    exchanges: list[tuple[int, int]] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for seen in pool.map(lambda i: _exchange_in_own_loop(key, i),
                             range(WORKERS)):
            exchanges.extend(seen)

    advanced = [prev for prev, mine in exchanges if mine > prev]
    duplicates = [v for v, n in Counter(advanced).items() if n > 1]

    assert advanced, "no exchange advanced the baseline; the test proved nothing"
    assert not duplicates, (
        f"{len(duplicates)} previous reading(s) handed to more than one "
        f"advancing caller, out of {len(advanced)}: overlapping deltas")


async def test_a_late_reading_cannot_roll_the_baseline_backwards(worker, key):
    """Reclaimed entries are old by definition and now flow through this path.

    A stale reading overwriting a fresher baseline would make the NEXT real
    sample measure against a reading from the future, get a negative interval
    and be discarded - the rate vanishes for that series rather than being
    slightly wrong, which is harder to notice.
    """
    await worker._exchange_baselines({key: "5000:9000000"})
    late = await worker._exchange_baselines({key: "1000:1000000"})

    assert late[key].observed_at_us == 9000000, (
        "the late caller should see the newer reading, and discard its own")

    after = await worker._exchange_baselines({key: "6000:10000000"})
    assert after[key] == rates.Baseline(value=5000, observed_at_us=9000000), (
        "the late reading overwrote a fresher baseline")


async def test_the_late_readings_rate_is_discarded_not_wrong(worker, key):
    """End to end: what the caller does with what it was handed."""
    await worker._exchange_baselines({key: "5000:9000000"})
    prev = (await worker._exchange_baselines({key: "1000:1000000"}))[key]

    rate, reason = rates.derive(
        metric="if_in_octets", instance="eth0", current=1000,
        observed_at_us=1000000, counter_bits=64, counter_reset=False,
        baseline=prev, max_gap_s=900)

    assert rate is None
    assert reason == rates.DiscardReason.NON_POSITIVE_DT


async def test_an_equal_timestamp_does_not_reinstall(worker, key):
    """A duplicate delivery is not new information.

    The reclaim path can hand the same entry over twice; installing it again
    would be harmless but the guard should be >=, and this pins it.
    """
    await worker._exchange_baselines({key: "1000:1000000"})
    again = await worker._exchange_baselines({key: "9999:1000000"})
    assert again[key].value == 1000
    after = await worker._exchange_baselines({key: "2000:2000000"})
    assert after[key].value == 1000, "a same-timestamp duplicate overwrote it"


async def test_a_batch_exchanges_every_series_it_carries(worker):
    """One round trip per batch, not per sample - and none of them crossed."""
    keys = {f"test:{uuid.uuid4().hex}|if_in_octets|eth{i}": f"{i * 10}:1000000"
            for i in range(25)}
    assert await worker._exchange_baselines(keys) == {}

    second = {k: f"{i * 100}:2000000" for i, k in enumerate(keys)}
    got = await worker._exchange_baselines(second)

    assert len(got) == 25
    for i, k in enumerate(keys):
        assert got[k].value == i * 10, f"{k} was handed another series' reading"
