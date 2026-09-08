"""Booleans are stored on change, plus a heartbeat.

A binary point - a breaker bit, a chiller's running flag, a port's oper
state - is polled like everything else but barely ever moves. Storing every
poll wrote 483,000 identical rows an hour for 12,000 series, and the table's
indexes outgrew its heap. Every historian does the same thing about this:
exception reporting with a zero deadband (a boolean has no noise to filter),
and a maximum interval after which the value is written again even if it has
not changed. BACnet's change-of-value subscription is the same idea on the
wire.

**The heartbeat is not optional.** Retention drops chunks by age, so a series
that never changed would eventually have no row at all, and every reader here
takes "the newest row inside a window" as the current state. The heartbeat
keeps one row per series inside every window; ``LAST_KNOWN_WINDOW_S`` is the
window readers must use, and it is twice the heartbeat so one late batch does
not make a healthy chiller vanish from the staging view.

**The decision is made in Redis, atomically, per series.** Two ingest workers
share one consumer group and a series has no permanent owner (see
``IngestWorker._exchange_baselines`` for why), so "have I already written this
value" cannot live in a worker's memory - each worker would write its own
first sight and its own heartbeats. One Lua step reads the last written state,
decides, and installs the new state only when a row will be written; the
worker that gets 1 writes, the other gets 0 and does not. A sample older than
the installed state is dropped as a replay, which is the same trade the
counter baselines make: one row lost to reordering rather than a change
recorded out of order.

Only the ROW is suppressed. Every sample still reaches the alarm rules (dwell
and clear need the repeats), the hot cache and the websocket, and still marks
its endpoint as producing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from app.ingest.writer import BoolRow

#: Longest an unchanged boolean goes without a fresh row. Fifteen minutes is
#: about eleven polls at this fleet's cadence, so the table shrinks ~90%, and
#: it keeps the last-known window short enough that a machine which stopped
#: reporting is not shown in its old state for hours.
BOOL_HEARTBEAT_S = 900

#: What a reader of "current state" must look back. Twice the heartbeat: a
#: heartbeat row that landed one batch late is still inside it.
LAST_KNOWN_WINDOW_S = 2 * BOOL_HEARTBEAT_S

#: The Redis state outlives the heartbeat by a day so a decommissioned series
#: cleans itself up, and a worker outage shorter than that resumes without a
#: burst of first-sight writes.
BOOL_STATE_TTL_S = 86_400

KEY_PREFIX = "dcim:bool:"

# ARGV[1] state ("1:good"), ARGV[2] observed epoch seconds, ARGV[3] heartbeat
# seconds, ARGV[4] ttl seconds. Returns 1 when the caller must write a row.
_ADMIT_LUA = """
local prev = redis.call('GET', KEYS[1])
local ts = tonumber(ARGV[2])
if prev then
  local pstate, pts = string.match(prev, '^(.*)@([^@]*)$')
  pts = tonumber(pts)
  if ts < pts then
    return 0
  end
  if pstate == ARGV[1] and ts - pts < tonumber(ARGV[3]) then
    return 0
  end
end
redis.call('SET', KEYS[1], ARGV[1] .. '@' .. ARGV[2], 'EX', ARGV[4])
return 1
"""


def state_key(device_id: str, metric_id: int, instance: str) -> str:
    return f"{KEY_PREFIX}{device_id}|{metric_id}|{instance}"


def encode_state(value: bool, quality: str) -> str:
    """Quality is part of the state: good -> suspect on the same value is a
    change worth a row, because it is what a reader would want to know."""
    return f"{1 if value else 0}:{quality}"


async def admit(redis: Redis, rows: list[BoolRow], *,
                heartbeat_s: int = BOOL_HEARTBEAT_S,
                ttl_s: int = BOOL_STATE_TTL_S) -> list[BoolRow]:
    """The subset of ``rows`` that must be written, in their original order.

    One Redis round trip for the whole batch. Rows for the same series inside
    one batch are decided in order, so a flip and flip-back within a batch
    yields both rows.
    """
    if not rows:
        return []
    script = redis.register_script(_ADMIT_LUA)
    pipe = redis.pipeline()
    for r in rows:
        # Awaited though buffered: on the async client the script call is a
        # coroutine, and left unawaited it queues nothing.
        await script(keys=[state_key(str(r.device_id), r.metric_id, r.instance)],
                     args=[encode_state(r.value, r.quality),
                           repr(r.ts.timestamp()), heartbeat_s, ttl_s],
                     client=pipe)
    verdicts = await pipe.execute()
    return [r for r, v in zip(rows, verdicts, strict=True) if v == 1]
