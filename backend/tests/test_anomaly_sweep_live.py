"""The sweep's lock and cadence gate, and the fit's session lock, on a real
database (docs/29 section 10.1, docs/33 Phase 0).

Skipped unless DCIM_TEST_DATABASE_URL is set, and needs migration 0111 there.
Every session's transaction is rolled back. Redis is an in-process fake: the
gate's behaviour is the point, and no test may write to a real Redis.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.analytics import sweep

DB_URL = os.getenv("DCIM_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run"),
    pytest.mark.asyncio,
]

T0 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


class FakeRedis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, ex=None):
        self.kv[key] = value

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key, a, b):
        self.lists[key] = self.lists.get(key, [])[a:b + 1]

    async def expire(self, key, ttl):
        return True

    async def lrange(self, key, a, b):
        return self.lists.get(key, [])[a:b + 1]


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(DB_URL, poolclass=None)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def two_sessions(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as a, maker() as b:
        ta, tb = await a.begin(), await b.begin()
        try:
            yield a, b
        finally:
            await ta.rollback()
            await tb.rollback()


async def _slow_sweep(session, now):
    await asyncio.sleep(0.3)
    return sweep.SweepResult(ran=True)


async def test_exactly_one_of_two_concurrent_sweeps_holds_the_lock(two_sessions):
    a, b = two_sessions
    redis = FakeRedis()  # no gate key: the lock alone must serialise
    ra, rb = await asyncio.gather(
        sweep.guarded_sweep(a, redis, consumer="ingest-1", sweep_s=60, now=T0,
                            sweep=_slow_sweep),
        sweep.guarded_sweep(b, redis, consumer="ingest-2", sweep_s=60, now=T0,
                            sweep=_slow_sweep))
    outcomes = sorted([("ran" if r.ran else r.skipped) for r in (ra, rb)])
    assert outcomes == ["lock_held", "ran"]
    hb = json.loads(redis.kv[sweep.SWEEP_HEARTBEAT_KEY])
    assert hb["consumer"] == ("ingest-1" if ra.ran else "ingest-2")


async def test_a_second_holder_30s_later_is_refused_by_the_cadence_gate(engine):
    redis = FakeRedis()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as a, a.begin():
        first = await sweep.guarded_sweep(a, redis, consumer="ingest-1", sweep_s=60, now=T0)
        assert first.ran
        await a.rollback()  # releases DCIMA, as a commit would
    async with maker() as b, b.begin():
        later = await sweep.guarded_sweep(b, redis, consumer="ingest-2", sweep_s=60,
                                          now=T0 + timedelta(seconds=30))
        assert not later.ran and later.skipped == "cadence"
        # Past 0.9 x sweep_s it runs again.
        again = await sweep.guarded_sweep(b, redis, consumer="ingest-2", sweep_s=60,
                                          now=T0 + timedelta(seconds=55))
        assert again.ran
        await b.rollback()


async def test_with_no_gate_key_it_still_runs_once(engine):
    redis = FakeRedis()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as a, a.begin():
        r = await sweep.guarded_sweep(a, redis, consumer="ingest-1", sweep_s=60, now=T0)
        await a.rollback()
    assert r.ran and r.skipped is None
    assert sweep.SWEEP_HEARTBEAT_KEY in redis.kv


async def test_the_real_empty_sweep_reads_the_catalogue(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as a, a.begin():
        r = await sweep.run_sweep(a, T0)
        await a.rollback()
    assert r.ran and r.series_scored == 0 and r.actions == [] and r.frames == []


async def test_the_fit_session_lock_admits_one_and_releases(engine):
    redis_a, redis_b = FakeRedis(), FakeRedis()
    seen: dict[str, sweep.FitResult] = {}

    async with engine.connect() as ca, engine.connect() as cb:
        async def fit_a(conn, now):
            # While A holds DCIMB, B is refused.
            seen["b"] = await sweep.guarded_fit(cb, redis_b, consumer="ingest-2",
                                                hour_utc=3, now=now)
            return sweep.FitResult(ran=True)

        seen["a"] = await sweep.guarded_fit(ca, redis_a, consumer="ingest-1", hour_utc=3,
                                            now=T0, fit=fit_a)
        assert seen["a"].ran
        assert seen["b"].skipped == "lock_held"

        # Released: B now gets the lock (and runs, its own Redis having no
        # record of today's pass).
        r = await sweep.guarded_fit(cb, redis_b, consumer="ingest-2", hour_utc=3, now=T0)
        assert r.ran

        # A's Redis says today's pass is done: the guard skips the repeat.
        r = await sweep.guarded_fit(ca, redis_a, consumer="ingest-1", hour_utc=3, now=T0)
        assert r.skipped == "already_fitted"
        hb = json.loads(redis_a.kv[sweep.BASELINE_HEARTBEAT_KEY])
        assert hb["fit_date"] == "2026-10-21"
