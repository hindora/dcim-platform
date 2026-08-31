"""The pipeline warning that fired three times an hour and was right none of them.

Measured on the running platform: ingest_lag_high raised at 89.8 s, 90.8 s and
106.8 s within one hour, each clearing inside two minutes, while the consumer
was advancing 66.7 s per 66 s of wall clock and sitting 0.3 s behind the newest
entry. The pipeline was working. The banner said the estate could not be
trusted.

Looking at why turned up two things that were actually broken.
"""

from __future__ import annotations

import msgpack
import pytest

from app.alarms import platform as rules

# ----------------------------------------------------------------- dwell

def _signals(lag: float, sustained: float) -> rules.Signals:
    return rules.Signals(ingest_lag_s=lag, ingest_lag_sustained_s=sustained,
                         telemetry_age_s=10.0, telemetry_present=True)


def _lag_findings(lag: float, sustained: float) -> list:
    return [f for f in rules.evaluate(_signals(lag, sustained))
            if f.alarm_type == "ingest_lag_high"]


def test_a_burst_that_drains_raises_nothing():
    """90 s of lag for 30 s is a queue doing its job."""
    assert _lag_findings(90.8, sustained=30.0) == []


def test_lag_that_persists_still_raises():
    """The dwell must not be a way of never alarming."""
    found = _lag_findings(90.8, sustained=rules.INGEST_LAG_DWELL_S + 1)
    assert len(found) == 1
    assert found[0].severity == "WARNING"


def test_the_dwell_boundary_is_inclusive_of_the_wait():
    assert _lag_findings(90.0, sustained=rules.INGEST_LAG_DWELL_S - 0.1) == []
    assert len(_lag_findings(90.0, sustained=rules.INGEST_LAG_DWELL_S)) == 1


def test_a_real_stall_is_not_delayed_out_of_existence():
    """The worst reading in the live table was 59763 s. It must still land."""
    found = _lag_findings(59763.4, sustained=rules.INGEST_LAG_DWELL_S)
    assert len(found) == 1
    assert found[0].severity == "CRITICAL"


def test_the_message_says_how_long_not_just_how_much():
    """A number with no duration reads as "right now, forever"."""
    found = _lag_findings(90.0, sustained=600.0)
    assert "10 min" in found[0].message


def test_lag_under_the_line_never_raises_however_long_it_lasts():
    assert _lag_findings(10.0, sustained=100_000.0) == []


# ------------------------------------------------------- reclaimed entries

class _FakeRedis:
    """Enough Redis to watch what the worker does with a claimed batch."""

    def __init__(self, claimable):
        self._claimable = dict(claimable)
        self.acked: list[tuple[str, str]] = []
        self.claim_calls = 0
        self.deleted_consumers: list[tuple[str, str]] = []
        self.consumers: dict[str, list[dict]] = {}

    async def xautoclaim(self, stream, group, consumer, min_idle_time, count):
        self.claim_calls += 1
        msgs = self._claimable.pop(stream, [])
        return (b"0-0", msgs, [])

    async def xack(self, stream, group, *ids):
        for i in ids:
            self.acked.append((stream, i))

    async def xinfo_consumers(self, stream, group):
        return self.consumers.get(stream, [])

    async def xgroup_delconsumer(self, stream, group, name):
        self.deleted_consumers.append((stream, name))


def _entry(mid: bytes, payload: dict):
    return (mid, {b"p": msgpack.packb(payload)})


@pytest.mark.asyncio
async def test_reclaimed_entries_are_processed_and_acked(monkeypatch):
    """The bug: claimed, then dropped.

    XAUTOCLAIM moved entries to this consumer and the return value was thrown
    away. The only read in the worker asks for `>`, which is new-messages-only,
    so a claimed entry could never be read again - it sat pending forever and
    was re-claimed every tick. On the live platform that was 141 endpoint state
    changes and 265 heartbeats taken from a dead worker and lost.
    """
    from app.contracts.messages_gen import Stream
    from app.ingest.worker import IngestWorker

    worker = IngestWorker.__new__(IngestWorker)
    worker.settings = type("S", (), {"ingest_group": "g",
                                     "ingest_claim_idle_ms": 1000})()
    worker.consumer = "me"
    worker.redis = _FakeRedis({Stream.ENDPOINTSTATE: [
        _entry(b"1-1", {"endpoint_id": "a"}), _entry(b"1-2", {"endpoint_id": "b"})]})

    seen: list[list[dict]] = []

    async def capture(payloads):
        seen.append(payloads)

    monkeypatch.setattr(worker, "_handle_endpoint_state", capture)
    await worker._reclaim_stale()

    assert seen and len(seen[0]) == 2, "claimed entries were not processed"
    assert worker.redis.acked == [(Stream.ENDPOINTSTATE, b"1-1"),
                                  (Stream.ENDPOINTSTATE, b"1-2")], (
        "processed entries must be acked or they stay pending forever")


@pytest.mark.asyncio
async def test_a_reclaimed_batch_does_not_set_the_pipeline_latency(monkeypatch):
    """Reclaimed telemetry is old by definition - that is why it was claimed.

    Letting it set the lag figure would report a dead worker's backlog as the
    live transport delay, and raise the very alarm this change exists to stop
    lying.
    """
    from app.contracts.messages_gen import Stream
    from app.ingest.worker import IngestWorker

    worker = IngestWorker.__new__(IngestWorker)
    worker.settings = type("S", (), {"ingest_group": "g",
                                     "ingest_claim_idle_ms": 1000})()
    worker.consumer = "me"
    worker._last_lag_s = None
    worker.redis = _FakeRedis({Stream.TELEMETRY: [
        _entry(b"1-1", {"sent_at": 1, "samples": []})]})

    async def noop(payloads):
        pass

    monkeypatch.setattr(worker, "_handle_telemetry", noop)
    monkeypatch.setattr(worker, "_trace", lambda *a, **k: None)
    await worker._reclaim_stale()

    assert worker._last_lag_s is None, (
        "an ancient reclaimed batch was reported as the current lag")


# --------------------------------------------------------- consumer reaping

@pytest.mark.asyncio
async def test_only_idle_and_empty_consumers_are_reaped():
    """Deleting a consumer that still holds pending entries destroys them.

    That is the failure this is meant to prevent, not cause. The live group had
    104 consumer records for one running worker - one per restart, each keeping
    its pending list forever and each walked by XAUTOCLAIM on every tick.
    """
    from app.contracts.messages_gen import Stream
    from app.ingest import worker as mod
    from app.ingest.worker import IngestWorker

    w = IngestWorker.__new__(IngestWorker)
    w.settings = type("S", (), {"ingest_group": "g"})()
    w.consumer = "live"
    w._last_consumer_reap = float("-inf")
    w.redis = _FakeRedis({})
    dead = mod.CONSUMER_DEAD_AFTER_MS
    for stream in (Stream.TELEMETRY, Stream.EVENTS, Stream.ENDPOINTSTATE,
                   Stream.HEARTBEAT):
        w.redis.consumers[stream] = [
            {"name": b"live", "pending": 0, "idle": dead + 1},
            {"name": b"old-empty", "pending": 0, "idle": dead + 1},
            {"name": b"old-holding-work", "pending": 7, "idle": dead + 1},
            {"name": b"recent", "pending": 0, "idle": 1000},
        ]

    await w._reap_dead_consumers()

    reaped = {name for _, name in w.redis.deleted_consumers}
    assert reaped == {"old-empty"}, f"reaped the wrong set: {reaped}"
