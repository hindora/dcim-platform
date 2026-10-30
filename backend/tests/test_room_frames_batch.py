"""A run of replay frames in one request is the same frames, built once.

The replay buffer asks for several consecutive moments at a time. What must
hold: each frame equals what `/frame?t=` returns for that moment, the room's
geometry is built once for the run, the alarms are read once, moments past
now are left out, and the per-moment alarm rule is the single-moment one.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.core import twin_frame as frame
from app.services import devices as svc
from tests.test_room_alarm_pins import _NOW, _stub

T0 = _NOW - timedelta(hours=3)


def _rows():
    """srv-1: a MAJOR open across the run and a CRITICAL raised mid-run;
    crah-1: a MINOR cleared mid-run; pdu-1: one cleared before the run."""
    return [
        {"device_id": "srv-1", "severity": "MAJOR", "first_seen": T0 - timedelta(hours=1),
         "cleared_at": None},
        {"device_id": "srv-1", "severity": "CRITICAL", "first_seen": T0 + timedelta(minutes=10),
         "cleared_at": T0 + timedelta(minutes=20)},
        {"device_id": "crah-1", "severity": "MINOR", "first_seen": T0 - timedelta(minutes=30),
         "cleared_at": T0 + timedelta(minutes=5)},
        {"device_id": "pdu-1", "severity": "WARNING", "first_seen": T0 - timedelta(hours=2),
         "cleared_at": T0 - timedelta(minutes=1)},
    ]


def test_severities_from_follow_the_single_moment_rule():
    rows = _rows()
    at = lambda m: frame.severities_from(rows, T0 + timedelta(minutes=m))  # noqa: E731
    assert at(0) == {"srv-1": "MAJOR", "crah-1": "MINOR"}
    # cleared exactly at t: not open at t (cleared_at > t is the rule)
    assert at(5) == {"srv-1": "MAJOR"}
    # raised exactly at t: open at t, and the worse of two wins
    assert at(10) == {"srv-1": "CRITICAL"}
    assert at(20) == {"srv-1": "MAJOR"}


def test_a_run_builds_the_room_once_and_matches_the_single_frame(monkeypatch):
    _stub(monkeypatch)
    calls = {"scene": 0, "alarms": 0, "values": []}
    real_scene = svc.room_scene

    async def counted_scene(s, room):
        calls["scene"] += 1
        return await real_scene(s, room)

    async def values_at(_s, *, t, **_kw):
        calls["values"].append(t)
        return {"srv-1": {"inlet_temperature": 20.0 + (t - T0).total_seconds() / 60.0}}

    async def alarms_spanning(_s, *, t0, t1, **_kw):
        calls["alarms"] += 1
        assert (t0, t1) == (T0, T0 + timedelta(minutes=15))
        return _rows()

    async def severities_at(_s, *, t, **_kw):
        return frame.severities_from(_rows(), t)

    monkeypatch.setattr(svc, "room_scene", counted_scene)
    monkeypatch.setattr(svc.tele_repo, "values_at", values_at)
    monkeypatch.setattr(svc.tele_repo, "alarms_spanning", alarms_spanning)
    monkeypatch.setattr(svc.tele_repo, "severities_at", severities_at)

    run = asyncio.run(svc.room_frames(None, "room-1", T0, 5, 4, now=_NOW))
    assert calls["scene"] == 1 and calls["alarms"] == 1
    assert [f.frame.t for f in run] == [T0 + timedelta(minutes=5 * k) for k in range(4)]

    for k, f in enumerate(run):
        at = T0 + timedelta(minutes=5 * k)
        single = asyncio.run(svc.room_frame(None, "room-1", at, now=_NOW))
        assert f.model_dump() == single.model_dump(), k
    srv = [next(d for d in f.devices if d.id == "srv-1") for f in run]
    assert [d.max_severity for d in srv] == ["MAJOR", "MAJOR", "CRITICAL", "CRITICAL"]


def test_moments_past_now_are_left_out_and_the_run_is_capped(monkeypatch):
    _stub(monkeypatch)

    async def values_at(_s, **_kw):
        return {}

    async def alarms_spanning(_s, **_kw):
        return []

    monkeypatch.setattr(svc.tele_repo, "values_at", values_at)
    monkeypatch.setattr(svc.tele_repo, "alarms_spanning", alarms_spanning)
    near = _NOW - timedelta(minutes=7)
    run = asyncio.run(svc.room_frames(None, "room-1", near, 5, 6, now=_NOW))
    assert [f.frame.t for f in run] == [near, near + timedelta(minutes=5)]
    run = asyncio.run(svc.room_frames(None, "room-1", T0, 1, 50, now=_NOW))
    assert len(run) == svc.MAX_FRAMES
    later = _NOW + timedelta(minutes=1)
    assert asyncio.run(svc.room_frames(None, "room-1", later, 5, 3, now=_NOW)) == []
