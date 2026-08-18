"""Publish post-commit changes to Redis pub/sub for the WebSocket layer.

Runs AFTER the database transaction commits. Publishing first would let a
browser see a value that then rolls back, and that is a bug users report as
"the dashboard lies".

Fan-out goes through Redis rather than an in-process registry because the API
runs with more than one uvicorn worker: a WebSocket session lives in one
process while ingest runs in another.
"""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis

from app.core.logging import get_logger

log = get_logger(__name__)

CHANNEL_PREFIX = "dcim:ws:"


def channel(topic: str) -> str:
    return f"{CHANNEL_PREFIX}{topic}"


class Fanout:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def telemetry(self, by_device: dict[str, dict[str, Any]]) -> None:
        """One frame per device. The WS layer coalesces further before sending."""
        if not by_device:
            return
        pipe = self._redis.pipeline()
        for device_id, metrics in by_device.items():
            frame = {"event": "telemetry_update", "device_id": device_id,
                     "metrics": metrics}
            pipe.publish(channel(f"device:{device_id}"), json.dumps(frame))
        try:
            await pipe.execute()
        except Exception as exc:
            log.warning("ws fanout failed", error=str(exc))

    async def device_status(self, device_id: str, status: str,
                            previous: str | None) -> None:
        frame = {"event": "device_status_change", "device_id": device_id,
                 "status": status, "previous": previous}
        await self._publish(f"device:{device_id}", frame)
        await self._publish("dashboard", frame)

    async def collector_status(self, collector_id: str, status: str,
                               detail: str | None = None) -> None:
        await self._publish("collectors", {
            "event": "collector_status", "collector_id": collector_id,
            "status": status, "detail": detail})

    async def _publish(self, topic: str, frame: dict) -> None:
        try:
            await self._redis.publish(channel(topic), json.dumps(frame))
        except Exception as exc:
            log.warning("ws fanout failed", topic=topic, error=str(exc))
