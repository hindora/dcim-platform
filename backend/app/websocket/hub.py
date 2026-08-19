"""WebSocket fan-out.

Two decisions shape this file:

* **Fan-out goes through Redis pub/sub, not an in-process registry.** A session
  lives in one uvicorn worker while the ingest worker runs in another process
  entirely, so anything in-process would deliver to a fraction of the browsers
  and look like flaky updates. One pattern subscription per process, never one
  per client.

* **Telemetry is coalesced; alarms are not.** 664 devices times ~40 metrics is
  ~26,000 values a cycle - no browser wants that, and no operator can read it.
  Telemetry is collapsed to the newest value per key on a one-second tick.
  Alarms and status changes bypass the coalescer: they are rare and latency is
  the whole point.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from starlette.websockets import WebSocket

from app.core.logging import get_logger

log = get_logger("ws")

CHANNEL_PREFIX = "dcim:ws:"
COALESCED_EVENTS = {"telemetry_update"}


# eq=False keeps the default identity hash: sessions live in sets keyed by
# topic, and a dataclass's generated __eq__ sets __hash__ to None, which makes
# them unhashable. Two connections are never "equal" anyway - they are distinct
# sockets even for the same user.
@dataclass(eq=False)
class Session:
    id: str
    ws: WebSocket
    topics: set[str] = field(default_factory=set)
    queue: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(256))


class ConnectionHub:
    def __init__(self, redis: Redis, coalesce_ms: int = 1000,
                 max_topics: int = 50) -> None:
        self._redis = redis
        self._coalesce = max(coalesce_ms, 100) / 1000
        self._max_topics = max_topics
        self._by_topic: dict[str, set[Session]] = {}
        self._sessions: dict[str, Session] = {}
        self._pending: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._subscribe_loop(), name="ws-subscribe"),
            asyncio.create_task(self._flush_loop(), name="ws-flush"),
        ]
        log.info("websocket hub started", coalesce_ms=int(self._coalesce * 1000))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks = []

    # ------------------------------------------------------------- sessions

    async def register(self, session: Session) -> None:
        async with self._lock:
            self._sessions[session.id] = session

    async def unregister(self, session: Session) -> None:
        async with self._lock:
            self._sessions.pop(session.id, None)
            for topic in session.topics:
                subs = self._by_topic.get(topic)
                if subs:
                    subs.discard(session)
                    if not subs:
                        self._by_topic.pop(topic, None)
            session.topics.clear()

    async def subscribe(self, session: Session, topics: list[str]) -> list[str]:
        accepted = []
        async with self._lock:
            for topic in topics:
                if len(session.topics) >= self._max_topics:
                    break
                session.topics.add(topic)
                self._by_topic.setdefault(topic, set()).add(session)
                accepted.append(topic)
        return accepted

    async def unsubscribe(self, session: Session, topics: list[str]) -> None:
        async with self._lock:
            for topic in topics:
                session.topics.discard(topic)
                subs = self._by_topic.get(topic)
                if subs:
                    subs.discard(session)
                    if not subs:
                        self._by_topic.pop(topic, None)

    # -------------------------------------------------------------- routing

    async def _subscribe_loop(self) -> None:
        while True:
            try:
                pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
                # ONE pattern subscription for the whole process. Subscribing
                # per client would open thousands of Redis subscriptions.
                await pubsub.psubscribe(f"{CHANNEL_PREFIX}*")
                async for msg in pubsub.listen():
                    if msg is None or msg.get("type") != "pmessage":
                        continue
                    channel = msg["channel"]
                    if isinstance(channel, bytes):
                        channel = channel.decode()
                    topic = channel[len(CHANNEL_PREFIX):]
                    data = msg["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    await self._route(topic, json.loads(data))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ws subscribe loop restarting", error=str(exc))
                await asyncio.sleep(1.0)

    async def _route(self, topic: str, frame: dict[str, Any]) -> None:
        if frame.get("event") in COALESCED_EVENTS:
            key = (topic, frame.get("device_id") or "")
            merged = self._pending.setdefault(key, {"event": frame["event"],
                                                    "device_id": frame.get("device_id"),
                                                    "metrics": {}})
            merged["metrics"].update(frame.get("metrics") or {})
            return
        await self._send(topic, frame)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._coalesce)
            if not self._pending:
                continue
            batch, self._pending = self._pending, {}
            for (topic, _), frame in batch.items():
                await self._send(topic, frame)

    async def _send(self, topic: str, frame: dict[str, Any]) -> None:
        async with self._lock:
            sessions = list(self._by_topic.get(topic, ()))
        if not sessions:
            return
        payload = json.dumps(frame).encode()
        for s in sessions:
            try:
                s.queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop the session rather than buffer without bound: one wedged
                # browser tab must not be able to exhaust the API's memory. The
                # client reconnects and re-syncs over REST.
                log.warning("slow consumer disconnected", session=s.id,
                            topic=topic)
                with contextlib.suppress(Exception):
                    await s.ws.close(code=1013)
                await self.unregister(s)

    # --------------------------------------------------------------- stats

    def stats(self) -> dict[str, int]:
        return {"sessions": len(self._sessions), "topics": len(self._by_topic)}


_hub: ConnectionHub | None = None


def get_hub() -> ConnectionHub | None:
    return _hub


def set_hub(hub: ConnectionHub | None) -> None:
    global _hub
    _hub = hub
