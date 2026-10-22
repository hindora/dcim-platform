"""Rolling windows and accumulators in Redis (docs/29 section 10).

`dcim:anomaly:win` holds, per series `{device_id}|{metric_id}|{instance}`,
the recent samples and the read cursor, so each sweep reads only samples
newer than the cursor (about 3.5k rows instead of 104k, docs/29 section 6.2).
A missing field - after a Redis loss, or a series never seen - backfills
30 min from telemetry on the next sweep.

Booleans are change-logged with a 900 s heartbeat (ingest/changelog.py), so
their window is not a sample list but a last-known value: the value, when it
began, whether that beginning is an observed TRANSITION or just the oldest
row in the look-back, and the 0 -> 1 transitions seen (CH-4's starts). A
heartbeat row repeats the value; it is never a start.

`dcim:anomaly:acc` holds the dwell state (rules.DwellState) per
`{detector_id}|{device_id}|{metric_id}|{instance}`.

Contract deviation (docs/29 section 10, "last <= 32 samples"): a window holds
up to 64 samples no older than 2 h. CR-6/VA-2 count reversals over 1800 s,
which is 60 samples at the 30 s BACnet cadence, and DQ-5 needs 20 samples,
which on a 120 s SNMP probe is 40 min. 64 x ~20 B x 2,330 series is ~3 MB.

Both hashes take a whole-hash TTL refreshed on every write (Redis 7.4's
per-field TTL cannot be assumed on the floating redis:7-alpine tag); fields
of series not seen for 24 h are deleted by the sweep.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.analytics.detectors.rules import BoolSeries, Sample

WIN_KEY = "dcim:anomaly:win"
ACC_KEY = "dcim:anomaly:acc"
TTL_S = 86_400

#: First read of a series, and the boolean last-known look-back
#: (ingest/changelog.py LAST_KNOWN_WINDOW_S).
BACKFILL_S = 1800
KEEP_MAX = 64
KEEP_S = 7200
#: CH-4's window can be widened to 4 h by PATCH; starts are kept that long.
STARTS_KEEP_S = 14_400
#: A cursor older than this does not hold back the device's read: it is a
#: dead series, and DQ-1 already knows its last sample.
LIVE_CURSOR_S = 600

_Q = {"good": "g", "suspect": "s", "bad": "b"}
_Q_BACK = {v: k for k, v in _Q.items()}


def series_field(device_id: str, metric_id: int, instance: str) -> str:
    return f"{device_id}|{metric_id}|{instance}"


@dataclass
class NumWindow:
    cursor: float | None = None
    samples: list[Sample] = field(default_factory=list)

    def to_wire(self) -> str:
        return json.dumps({"k": "n", "c": self.cursor,
                           "s": [[round(s.ts, 3), s.v, _Q.get(s.q, "g")]
                                 for s in self.samples]}, separators=(",", ":"))


@dataclass
class BoolWindow:
    cursor: float | None = None
    series: BoolSeries = field(default_factory=BoolSeries)

    def to_wire(self) -> str:
        b = self.series
        return json.dumps({"k": "b", "c": self.cursor,
                           "v": None if b.value is None else int(b.value), "t": b.last_ts,
                           "si": b.since, "sk": int(b.since_known),
                           "st": [round(t, 3) for t in b.starts]}, separators=(",", ":"))


def parse(raw: str | bytes | None) -> NumWindow | BoolWindow | None:
    if raw is None:
        return None
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    if d.get("k") == "b":
        v = d.get("v")
        return BoolWindow(d.get("c"), BoolSeries(
            value=None if v is None else bool(v), last_ts=d.get("t"), since=d.get("si"),
            since_known=bool(d.get("sk")), starts=list(d.get("st") or [])))
    return NumWindow(d.get("c"), [Sample(float(t), float(v), _Q_BACK.get(q, "good"))
                                  for t, v, q in d.get("s") or []])


def merge_numeric(win: NumWindow, rows: Iterable[tuple[float, float, str]],
                  now: float) -> tuple[NumWindow, bool]:
    """Append rows newer than the cursor; trim. Returns (window, got_new)."""
    got = False
    for ts, v, q in sorted(rows):
        if win.cursor is not None and ts <= win.cursor:
            continue
        win.samples.append(Sample(ts, v, q))
        win.cursor = ts
        got = True
    keep = [s for s in win.samples if now - s.ts <= KEEP_S]
    if not keep and win.samples:
        keep = win.samples[-1:]          # DQ-1 needs the last sample's age
    win.samples = keep[-KEEP_MAX:]
    return win, got


def merge_bool(win: BoolWindow, rows: Iterable[tuple[float, bool, str]],
               now: float) -> tuple[BoolWindow, bool]:
    """Fold change-logged rows into the last-known value.

    The first row ever seen has no known predecessor: it sets the value but is
    not a transition, so neither a heartbeat nor the backfill's oldest row is
    ever counted as a start.
    """
    got = False
    b = win.series
    for ts, v, q in sorted(rows):
        if win.cursor is not None and ts <= win.cursor:
            continue
        if q == "bad":
            win.cursor = ts
            continue                      # a faulted point says nothing
        if b.value is None:
            b.value, b.since, b.since_known = v, ts, False
        elif v != b.value:
            if v and not b.value:
                b.starts.append(ts)
            b.value, b.since, b.since_known = v, ts, True
        b.last_ts = ts
        win.cursor = ts
        got = True
    b.starts = [t for t in b.starts if now - t <= STARTS_KEEP_S]
    return win, got


def device_since(cursors: list[float | None], now: float) -> float:
    """Where a device's incremental read starts: the oldest LIVE cursor."""
    floor = now - BACKFILL_S
    live = [c for c in cursors if c is not None and now - c <= LIVE_CURSOR_S]
    if not cursors or None in cursors or not live:
        return floor
    return max(floor, min(live))


class HashStore:
    """One Redis hash, read whole once per sweep and written back by diff.

    With no Redis (tests, a dry run) it is an in-process dict, so the sweep
    logic is the same either way.
    """

    def __init__(self, redis: Any, key: str, ttl_s: int = TTL_S) -> None:
        self.redis, self.key, self.ttl_s = redis, key, ttl_s
        self.data: dict[str, str] = {}
        self._dirty: dict[str, str] = {}
        self._deleted: set[str] = set()

    async def load(self) -> dict[str, str]:
        if self.redis is None:
            self.data = dict(_MEMORY.setdefault(self.key, {}))
            return self.data
        raw = await self.redis.hgetall(self.key) or {}
        self.data = {(k.decode() if isinstance(k, bytes) else k):
                     (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}
        return self.data

    def get(self, field_: str) -> str | None:
        return self.data.get(field_)

    def put(self, field_: str, value: str) -> None:
        if self.data.get(field_) != value:
            self.data[field_] = value
            self._dirty[field_] = value
            self._deleted.discard(field_)

    def delete(self, field_: str) -> None:
        if field_ in self.data:
            del self.data[field_]
            self._dirty.pop(field_, None)
            self._deleted.add(field_)

    async def flush(self) -> int:
        n = len(self._dirty) + len(self._deleted)
        if self.redis is None:
            mem = _MEMORY.setdefault(self.key, {})
            mem.update(self._dirty)
            for f in self._deleted:
                mem.pop(f, None)
        elif n:
            if self._dirty:
                await self.redis.hset(self.key, mapping=self._dirty)
            if self._deleted:
                await self.redis.hdel(self.key, *sorted(self._deleted))
            await self.redis.expire(self.key, self.ttl_s)
        self._dirty, self._deleted = {}, set()
        return n


#: The no-Redis store. Process-local and only ever used without a client.
_MEMORY: dict[str, dict[str, str]] = {}


def reset_memory() -> None:
    _MEMORY.clear()
