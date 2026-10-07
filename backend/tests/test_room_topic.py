"""The per-room websocket topic (docs/27 Phase 3).

A room viewer cannot subscribe to every device in a hall under the 50-topic
session limit, so the ingest worker publishes one `room:{id}` frame per room
per batch, carrying only the fact that readings arrived.
"""

from __future__ import annotations

from pathlib import Path

from app.ingest.worker import group_by_room


def test_devices_group_by_the_room_they_stand_in():
    room_of = {"srv1": "hall-a", "srv2": "hall-a", "chl1": "plant", "ghost": None}
    out = group_by_room(["srv1", "chl1", "srv2", "unknown", "ghost"], room_of)
    assert out == {"hall-a": ["srv1", "srv2"], "plant": ["chl1"]}


def test_a_device_with_no_room_is_not_published_anywhere():
    assert group_by_room(["x"], {}) == {}


def test_room_frames_carry_no_readings():
    """The viewer re-reads the scene over REST on a frame; a delta stream with
    no replay must never be the thing it draws from."""
    src = (Path(__file__).resolve().parents[1] / "app" / "ingest" / "fanout.py").read_text(
        encoding="utf-8")
    start = src.index("async def room_updates(")
    body = src[start:src.index("async def device_status(", start)]
    assert '"event": "room_update"' in body
    assert "metrics" not in body
