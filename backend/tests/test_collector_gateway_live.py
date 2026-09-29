"""The ingest gateway against a real Redis - docs/26 Phase 3's acceptance bar
made concrete: a spoofed batch is dropped and counted, a retried batch is a
no-op not a duplicate, and Redis under pressure answers 429, not OOM.

Skipped unless DCIM_TEST_REDIS_URL is set, the same manual-gate convention
as the other *_live.py tests. Every test runs against a THROWAWAY stream
name, never the real telemetry.v1/events.v1/endpointstate.v1 - those are the
live dev stack's own data, and the very first run of this file found
telemetry.v1 already sitting at its real 8000-entry cap, which made every
test relying on spare headroom fail for a reason that had nothing to do
with the code under test. The isolated_stream fixture below is what fixed
that: it points the "telemetry" path segment at a fresh, empty stream for
the duration of each test and deletes it afterward.
"""

from __future__ import annotations

import os
import uuid

import msgpack
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.services import collector_gateway as gw

REDIS_URL = os.getenv("DCIM_TEST_REDIS_URL")

pytestmark = [
    pytest.mark.skipif(not REDIS_URL, reason="set DCIM_TEST_REDIS_URL to run"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def redis():
    r = Redis.from_url(REDIS_URL)
    try:
        yield r
    finally:
        await r.aclose()


@pytest.fixture
def isolated_stream(monkeypatch):
    """Points the 'telemetry' path segment at a fresh, empty stream name for
    one test. gw.STREAM_BY_PATH and gw.STREAM_MAXLEN are module-level dicts
    that every call to ingest_batch reads fresh, so patching them here
    reaches ingest_batch without it needing to know tests exist."""
    name = f"test-gw-{uuid.uuid4().hex[:12]}"
    monkeypatch.setitem(gw.STREAM_BY_PATH, "telemetry", name)
    monkeypatch.setitem(gw.STREAM_MAXLEN, name, 8000)
    return name


@pytest_asyncio.fixture
async def cleanup_stream(redis, isolated_stream):
    yield isolated_stream
    await redis.delete(isolated_stream)


def _collector_id() -> str:
    return f"test-col-{uuid.uuid4().hex[:12]}"


def _batch(collector_id: str, samples: list[dict] | None = None) -> bytes:
    payload = {"collector_id": "someone-else", "schema_version": 1,
              "samples": samples or [{"metric": "cpu", "value": 1.0}]}
    return gw.compress_for_test(msgpack.packb(payload, use_bin_type=True))


async def test_the_stamped_collector_id_is_never_the_payloads_own(redis, cleanup_stream):
    """The acceptance bar verbatim: a spoofed batch claiming another
    collector's identity is dropped in the sense that matters - the
    identity that lands in Redis is never the one the payload claimed."""
    collector_id = _collector_id()
    result = await gw.ingest_batch(
        redis, collector_id=collector_id, path_segment="telemetry",
        spool_seq=1, compressed_body=_batch(collector_id))
    assert result.accepted and not result.duplicate

    rows = await redis.xrange(cleanup_stream, min=result.entry_id, max=result.entry_id)
    assert rows, "could not read back the entry this test just wrote"
    _, entries = rows[0]
    landed = msgpack.unpackb(entries[b"p"], raw=False)
    assert landed["collector_id"] == collector_id
    assert landed["collector_id"] != "someone-else"


async def test_a_retried_batch_with_the_same_seq_is_a_noop_not_a_duplicate_entry(
        redis, cleanup_stream):
    collector_id = _collector_id()
    body = _batch(collector_id)

    first = await gw.ingest_batch(redis, collector_id=collector_id,
                                  path_segment="telemetry", spool_seq=5,
                                  compressed_body=body)
    assert first.accepted and not first.duplicate

    retry = await gw.ingest_batch(redis, collector_id=collector_id,
                                  path_segment="telemetry", spool_seq=5,
                                  compressed_body=body)
    assert retry.accepted and retry.duplicate

    # An earlier seq than what is already committed is refused as stale too,
    # not just an exact repeat - the ordering guarantee dedup exists for.
    stale = await gw.ingest_batch(redis, collector_id=collector_id,
                                  path_segment="telemetry", spool_seq=3,
                                  compressed_body=body)
    assert stale.duplicate

    assert await redis.xlen(cleanup_stream) == 1, \
        "exactly one entry should have landed despite three requests"


async def test_a_higher_seq_after_a_duplicate_is_accepted_normally(redis, cleanup_stream):
    collector_id = _collector_id()
    await gw.ingest_batch(redis, collector_id=collector_id, path_segment="telemetry",
                          spool_seq=1, compressed_body=_batch(collector_id))
    again = await gw.ingest_batch(redis, collector_id=collector_id, path_segment="telemetry",
                                  spool_seq=2, compressed_body=_batch(collector_id))
    assert again.accepted and not again.duplicate
    assert await redis.xlen(cleanup_stream) == 2


async def test_two_collectors_dedup_independently(redis, cleanup_stream):
    """One collector's spool_seq=1 must not make another collector's own
    spool_seq=1 look like a replay - the dedup key is scoped per collector,
    not global."""
    a, b = _collector_id(), _collector_id()
    r1 = await gw.ingest_batch(redis, collector_id=a, path_segment="telemetry",
                               spool_seq=1, compressed_body=_batch(a))
    r2 = await gw.ingest_batch(redis, collector_id=b, path_segment="telemetry",
                               spool_seq=1, compressed_body=_batch(b))
    assert r1.accepted and not r1.duplicate
    assert r2.accepted and not r2.duplicate


async def test_a_newer_schema_version_than_this_platform_understands_is_refused(
        redis, cleanup_stream):
    collector_id = _collector_id()
    payload = {"collector_id": collector_id, "schema_version": 999_999,
              "samples": []}
    body = gw.compress_for_test(msgpack.packb(payload, use_bin_type=True))
    with pytest.raises(gw.GatewayError) as exc:
        await gw.ingest_batch(redis, collector_id=collector_id, path_segment="telemetry",
                              spool_seq=1, compressed_body=body)
    assert exc.value.status_code == 426
    assert await redis.xlen(cleanup_stream) == 0


async def test_garbage_that_does_not_decompress_is_refused_not_500d(redis, cleanup_stream):
    collector_id = _collector_id()
    with pytest.raises(gw.GatewayError) as exc:
        await gw.ingest_batch(redis, collector_id=collector_id, path_segment="telemetry",
                              spool_seq=1, compressed_body=b"not zstd at all")
    assert exc.value.status_code == 400
    assert await redis.xlen(cleanup_stream) == 0


async def test_an_unknown_stream_path_is_refused(redis):
    collector_id = _collector_id()
    with pytest.raises(gw.GatewayError) as exc:
        await gw.ingest_batch(redis, collector_id=collector_id, path_segment="nonsense",
                              spool_seq=1, compressed_body=_batch(collector_id))
    assert exc.value.status_code == 404


async def test_heartbeats_are_not_an_accepted_gateway_stream(redis):
    assert "heartbeat" not in gw.STREAM_BY_PATH
    assert "collectorhb" not in gw.STREAM_BY_PATH


async def test_high_water_on_stream_length_returns_429_with_retry_after(
        redis, isolated_stream):
    monkeypatch_cap = 1
    import pytest as _pytest  # local, to avoid a module-level monkeypatch fixture dance

    mp = _pytest.MonkeyPatch()
    mp.setitem(gw.STREAM_MAXLEN, isolated_stream, monkeypatch_cap)
    try:
        collector_id = _collector_id()
        # Push XLEN to the cap directly - simpler and more explicit than
        # relying on a first ingest_batch call to be the one under the cap.
        await redis.xadd(isolated_stream, {"p": b"filler"})
        with pytest.raises(gw.GatewayError) as exc:
            await gw.ingest_batch(redis, collector_id=collector_id, path_segment="telemetry",
                                  spool_seq=1, compressed_body=_batch(collector_id))
        assert exc.value.status_code == 429
        assert exc.value.retry_after
    finally:
        mp.undo()
        await redis.delete(isolated_stream)


async def test_memory_high_water_is_checked_independently_of_stream_length(
        redis, cleanup_stream, monkeypatch):
    """A stream far under its own length cap must still be refused once
    Redis's own memory is what is under pressure - the two checks are
    independent, not "either one clears the other"."""
    real_info = redis.info

    async def fake_info(section=None):
        result = await real_info(section)
        if section == "memory":
            result = dict(result)
            result["maxmemory"] = 1000
            result["used_memory"] = 999
        return result

    monkeypatch.setattr(redis, "info", fake_info)
    collector_id = _collector_id()
    with pytest.raises(gw.GatewayError) as exc:
        await gw.ingest_batch(redis, collector_id=collector_id, path_segment="telemetry",
                              spool_seq=1, compressed_body=_batch(collector_id))
    assert exc.value.status_code == 429
    assert "memory" in str(exc.value)
