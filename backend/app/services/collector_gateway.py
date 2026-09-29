"""The ingest gateway: where a remote collector's batches actually land.

docs/26 Phase 3. Until now every collector wrote straight to Redis with the
platform's own password - workable for a collector on the same trusted
network, a real liability for one reached over the internet. This is what
that collector talks to instead: HTTPS only, authenticated the same way
every other collector route is (app/core/security.require_collector - mTLS
preferred, the legacy bearer token still honoured), and the ONE thing this
module exists to guarantee - the collector_id on every record XADDed here is
the one `require_collector` proved, never whatever the payload itself
claims. A spoofed batch is not merely rejected; the request already carries
no other identity to fall back to.

What this does NOT change: the message a collector builds is the identical
TelemetryBatch/EventBatch/EndpointState/CollectorHeartbeat msgpack it always
built for a direct XADD (contracts/schema/messages_v1.yaml) - only the
transport moved, from a raw Redis command to a compressed HTTPS POST. The
ingest worker downstream reads the same four streams exactly as before and
does not know or care which path a given entry arrived by.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import msgpack
import zstandard
from redis.asyncio import Redis

from app.contracts.messages_gen import SCHEMA_VERSION, STREAM_MAXLEN, Stream
from app.core.logging import get_logger

log = get_logger("collector_gateway")

#: The path segment -> the Redis stream it XADDs to. A collector's URL says
#: "telemetry", never the wire stream name - one fewer thing a client has to
#: get byte-for-byte right, and one less place the stream name could leak
#: into a URL that gets logged somewhere less careful about secrets than
#: this platform is.
STREAM_BY_PATH: dict[str, str] = {
    "telemetry": Stream.TELEMETRY,
    "events": Stream.EVENTS,
    "endpointstate": Stream.ENDPOINTSTATE,
    # Heartbeats are deliberately NOT accepted here. A heartbeat is a
    # snapshot of "right now" - see internal/spool's own docstring on why it
    # is never spooled - and gains nothing from a durable, retryable POST;
    # collector_heartbeat.go's existing publish path covers it.
}

#: Redis memory, and the target stream's own length, past which the
#: gateway refuses new data rather than let either run out from under it.
#: 0.9 leaves headroom for the XADDs already in flight to land.
HIGH_WATER_FRACTION = 0.9
DEFAULT_RETRY_AFTER_S = 5

#: How long a dedup entry is remembered. Long enough that a collector
#: retrying across a real network hiccup (seconds, not hours) still lands on
#: the same key; short enough that a per-(collector, stream) key does not
#: accumulate forever for a fleet that grows over the platform's lifetime.
DEDUP_TTL_S = 3600

_DEDUP_CAS = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local incoming = tonumber(ARGV[1])
if incoming <= current then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""


class GatewayError(Exception):
    """A batch was refused. status_code and retry_after tell the router how."""

    def __init__(self, status_code: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass
class IngestResult:
    stream: str
    accepted: bool
    duplicate: bool
    entries: int
    #: The Redis-assigned entry id, for a caller (a test, an admin tool)
    #: that needs to read back exactly what was just written rather than
    #: "whatever is newest" on a stream other collectors and the ingest
    #: worker are using concurrently. None for a duplicate - nothing new
    #: was written.
    entry_id: str | None = None


def stream_key(path_segment: str) -> str:
    key = STREAM_BY_PATH.get(path_segment)
    if key is None:
        raise GatewayError(404, f"no such batch stream {path_segment!r}")
    return key


def _dedup_key(collector_id: str, stream: str) -> str:
    return f"dcim:gw:seq:{collector_id}:{stream}"


async def _already_seen(redis: Redis, collector_id: str, stream: str,
                        spool_seq: int) -> bool:
    """A plain read, not the commit - see _commit_seq for why the two are
    separate calls rather than one atomic op around the XADD itself."""
    current = await redis.get(_dedup_key(collector_id, stream))
    return current is not None and spool_seq <= int(current)


async def _commit_seq(redis: Redis, collector_id: str, stream: str, spool_seq: int) -> None:
    """Records spool_seq as done, AFTER the XADD it protects has actually
    landed - committing before the write would let a crash between the two
    calls make a real retry look like a duplicate and silently drop data
    that was never actually delivered. The Lua script's compare-and-set
    still matters even called after the fact: it stops a straggling retry
    with an OLDER seq from clobbering a newer one two concurrent requests
    already raced past each other."""
    key = _dedup_key(collector_id, stream)
    await redis.eval(_DEDUP_CAS, 1, key, str(spool_seq), str(DEDUP_TTL_S))


async def _check_high_water(redis: Redis, key: str) -> None:
    info = await redis.info("memory")
    used = int(info.get("used_memory", 0) or 0)
    maxmem = int(info.get("maxmemory", 0) or 0)
    if maxmem and used / maxmem >= HIGH_WATER_FRACTION:
        log.warning("gateway backpressure: redis memory high water",
                    used=used, maxmem=maxmem)
        raise GatewayError(429, "the platform is under memory pressure; retry shortly",
                           retry_after=DEFAULT_RETRY_AFTER_S)
    cap = STREAM_MAXLEN.get(key)
    if cap:
        length = await redis.xlen(key)
        if length / cap >= HIGH_WATER_FRACTION:
            log.warning("gateway backpressure: stream length high water",
                        stream=key, length=length, cap=cap)
            raise GatewayError(429, f"stream {key} is near capacity; retry shortly",
                               retry_after=DEFAULT_RETRY_AFTER_S)


_decompressor = zstandard.ZstdDecompressor()


def _decompress(body: bytes) -> bytes:
    try:
        return _decompressor.decompress(body, max_output_size=64 << 20)
    except zstandard.ZstdError as exc:
        raise GatewayError(400, f"could not decompress batch: {exc}") from None


async def ingest_batch(redis: Redis, *, collector_id: str, path_segment: str,
                       spool_seq: int, compressed_body: bytes) -> IngestResult:
    """Decompress, validate, stamp, and (if new) XADD one collector's batch.

    Order matters and is deliberate: identity and dedup are checked BEFORE
    the body is even decompressed, so an attacker cannot spend this
    process's CPU decompressing garbage without first holding a token that
    scopes it to a real, un-decommissioned collector - `require_collector`
    already ran by the time this is called.
    """
    key = stream_key(path_segment)

    if await _already_seen(redis, collector_id, key, spool_seq):
        log.info("gateway: duplicate batch, skipped", collector_id=collector_id,
                 stream=key, spool_seq=spool_seq)
        return IngestResult(stream=key, accepted=True, duplicate=True, entries=0,
                            entry_id=None)

    await _check_high_water(redis, key)

    raw = _decompress(compressed_body)
    try:
        payload = msgpack.unpackb(raw, raw=False)
    except Exception as exc:
        raise GatewayError(400, f"could not decode batch: {exc}") from None
    if not isinstance(payload, dict):
        raise GatewayError(400, "batch payload must be a map")

    wire_version = payload.get("schema_version", 0)
    if wire_version and wire_version > SCHEMA_VERSION:
        # A NEWER collector than this platform understands - the only
        # direction that is actually unsafe to accept. An older collector
        # (lower schema_version) is handled the same way it always has been:
        # whatever fields it omits decode to their zero value.
        raise GatewayError(426, f"batch schema_version {wire_version} is newer than "
                                f"this platform understands ({SCHEMA_VERSION})")

    # The one line this whole module exists for: the payload's own
    # collector_id, whatever it claims, is overwritten with the identity
    # require_collector actually proved. Every downstream reader - the
    # ingest worker's ownership check from docs/26 Phase 0 included - trusts
    # this field, so it must never be able to disagree with who this
    # connection authenticated as.
    payload["collector_id"] = collector_id

    entry_count = _entry_count(key, payload)
    reencoded = msgpack.packb(payload, use_bin_type=True)
    entry_id = await redis.xadd(key, {"p": reencoded}, maxlen=STREAM_MAXLEN.get(key),
                                approximate=True)

    await _commit_seq(redis, collector_id, key, spool_seq)
    log.info("gateway: batch accepted", collector_id=collector_id, stream=key,
             spool_seq=spool_seq, entries=entry_count)
    return IngestResult(stream=key, accepted=True, duplicate=False, entries=entry_count,
                        entry_id=entry_id.decode() if isinstance(entry_id, bytes)
                        else entry_id)


def _entry_count(stream: str, payload: dict[str, Any]) -> int:
    # telemetry.v1 and events.v1 carry a batch (a "samples"/"events" list);
    # endpointstate.v1 is published one state change at a time and always
    # counts as exactly one entry when accepted.
    field = {Stream.TELEMETRY: "samples", Stream.EVENTS: "events"}.get(stream)
    if field is None:
        return 1
    return len(payload.get(field) or [])


def compress_for_test(raw: bytes) -> bytes:
    """zstandard.ZstdCompressor().compress - exported under this name so
    tests build a request body without duplicating the compressor
    construction the real client (the Go collector) does independently."""
    return zstandard.ZstdCompressor().compress(raw)


def now_micros() -> int:
    return int(time.time() * 1_000_000)


__all__ = [
    "STREAM_BY_PATH",
    "GatewayError",
    "IngestResult",
    "compress_for_test",
    "ingest_batch",
    "stream_key",
]
