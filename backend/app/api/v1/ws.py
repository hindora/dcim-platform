"""WebSocket endpoint and its single-use ticket.

Browsers cannot set headers on a WebSocket handshake, so the token has to travel
in the query string - where it lands in proxy logs and browser history. The
session JWT therefore never goes near it: the client exchanges it for a
single-use ticket valid for sixty seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import Principal, current_principal
from app.websocket.hub import Session, get_hub

router = APIRouter(tags=["websocket"])
log = get_logger("api.ws")

TICKET_PREFIX = "dcim:wsticket:"
TICKET_TTL_S = 60


@router.post("/ws/ticket", summary="Single-use ticket for the WebSocket handshake")
async def issue_ticket(
    principal: Principal = Depends(current_principal),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    ticket = secrets.token_urlsafe(32)
    redis = Redis.from_url(settings.redis_url)
    try:
        await redis.set(f"{TICKET_PREFIX}{ticket}",
                        json.dumps({"sub": principal.username, "role": principal.role}),
                        ex=TICKET_TTL_S)
    finally:
        await redis.aclose()
    return {"ticket": ticket, "expires_in": TICKET_TTL_S}


async def _redeem(redis: Redis, ticket: str) -> dict[str, Any] | None:
    """Redeem once. GETDEL so a leaked ticket cannot be replayed."""
    raw = await redis.getdel(f"{TICKET_PREFIX}{ticket}")
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, ticket: str = "") -> None:
    settings = get_settings()
    hub = get_hub()
    if hub is None:
        await ws.close(code=1011)
        return

    redis = Redis.from_url(settings.redis_url)
    try:
        claims = await _redeem(redis, ticket) if ticket else None
    finally:
        await redis.aclose()

    if claims is None:
        await ws.close(code=1008)
        return

    await ws.accept()
    session = Session(id=secrets.token_hex(8), ws=ws)
    await hub.register(session)
    log.info("ws connected", session=session.id, user=claims.get("sub"))

    await ws.send_json({
        "event": "hello", "session_id": session.id,
        "protocol_version": 1, "heartbeat_interval_s": 30,
    })

    async def pump() -> None:
        """Drain the session queue to the socket."""
        while True:
            payload = await session.queue.get()
            await ws.send_bytes(payload)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive_json()
            op = msg.get("op")
            if op == "subscribe":
                topics = [t for t in msg.get("topics", []) if isinstance(t, str)]
                accepted = await hub.subscribe(session, topics)
                await ws.send_json({"event": "subscribed", "topics": accepted})
                if len(accepted) < len(topics):
                    await ws.send_json({
                        "event": "error", "code": "topic_limit",
                        "message": f"at most {settings.ws_max_topics} topics",
                    })
            elif op == "unsubscribe":
                await hub.unsubscribe(session, msg.get("topics", []))
            elif op == "ping":
                await ws.send_json({"event": "pong", "ts": msg.get("ts")})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("ws error", session=session.id, error=str(exc))
    finally:
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task
        await hub.unregister(session)
        log.info("ws disconnected", session=session.id)
