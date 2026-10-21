"""The room and rack 3D views mark open alarms the way the building view does.

`room_scene` rolls the room's open alarms up per device and per rack, so a
rack carries the category of the worst alarm on anything mounted in it and how
many are open, and a plant unit or a racked device carries its own. A past
frame knows only the severity at that moment, so it carries neither.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.schemas import FloorEquipment, FloorPlan, FloorRack, RoomExtent
from app.services import devices as svc


def _plan() -> FloorPlan:
    return FloorPlan(
        room_id="room-1", room_name="Server Hall A",
        extent=RoomExtent(width_m=12.0, depth_m=10.0),
        racks=[FloorRack(id="rack-1", name="R1-01", x=2.0, y=3.0, device_count=2, offline_count=0,
                         max_severity="MAJOR"),
               FloorRack(id="rack-2", name="R1-02", x=3.0, y=3.0, device_count=1, offline_count=0)],
        equipment=[FloorEquipment(id="crah-1", name="CRAH1", device_type="crah", x=8.0, y=5.0,
                                  w_m=2.0, d_m=0.9, max_severity="MINOR")],
        unpositioned_equipment=[], aisles=[],
    )


_DEVICES = [
    {"id": "srv-1", "name": "SRV01", "device_type": "server", "rack_id": "rack-1", "u_start": 10,
     "u_height": 2, "max_severity": "MAJOR"},
    {"id": "pdu-1", "name": "PDU-A", "device_type": "pdu", "rack_id": "rack-1", "u_start": None,
     "u_height": 0, "mount": "zero_u", "max_severity": "WARNING"},
    {"id": "srv-2", "name": "SRV02", "device_type": "server", "rack_id": "rack-2", "u_start": 4,
     "u_height": 1, "max_severity": "CLEAR"},
]

_NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
_ALARMS = [
    {"device_id": "srv-1", "rack_id": "rack-1", "room_id": "room-1", "category": "thermal",
     "severity": "MAJOR", "last_seen": _NOW},
    {"device_id": "srv-1", "rack_id": "rack-1", "room_id": "room-1", "category": "it",
     "severity": "MINOR", "last_seen": _NOW},
    {"device_id": "pdu-1", "rack_id": "rack-1", "room_id": "room-1", "category": "power",
     "severity": "WARNING", "last_seen": _NOW - timedelta(minutes=5)},
    {"device_id": "crah-1", "rack_id": None, "room_id": "room-1", "category": "cooling",
     "severity": "MINOR", "last_seen": _NOW},
]


def _stub(monkeypatch) -> None:
    async def floorplan(_s, _room):
        return _plan()

    async def rack_devices(_s, _room):
        return [dict(d) for d in _DEVICES]

    async def marks(_s, room):
        assert room == "room-1"
        return [dict(a) for a in _ALARMS]

    monkeypatch.setattr(svc, "room_floorplan", floorplan)
    monkeypatch.setattr(svc.rack_repo, "room_rack_devices", rack_devices)
    monkeypatch.setattr(svc.rack_repo, "room_alarm_marks", marks)


def test_a_rack_carries_the_worst_category_and_the_count_of_everything_in_it(monkeypatch):
    _stub(monkeypatch)
    scene = asyncio.run(svc.room_scene(None, "room-1"))
    rack = next(r for r in scene.plan.racks if r.id == "rack-1")
    assert rack.alarm_category == "thermal"      # the MAJOR wins over MINOR and WARNING
    assert rack.alarm_count == 3                 # two on the server, one on the PDU


def test_a_device_and_a_plant_unit_carry_their_own(monkeypatch):
    _stub(monkeypatch)
    scene = asyncio.run(svc.room_scene(None, "room-1"))
    by_id = {d.id: d for d in scene.devices}
    assert (by_id["srv-1"].alarm_category, by_id["srv-1"].alarm_count) == ("thermal", 2)
    assert (by_id["pdu-1"].alarm_category, by_id["pdu-1"].alarm_count) == ("power", 1)
    crah = scene.plan.equipment[0]
    assert (crah.alarm_category, crah.alarm_count) == ("cooling", 1)


def test_something_with_no_open_alarm_carries_nothing(monkeypatch):
    _stub(monkeypatch)
    scene = asyncio.run(svc.room_scene(None, "room-1"))
    quiet = next(r for r in scene.plan.racks if r.id == "rack-2")
    assert quiet.alarm_category is None and quiet.alarm_count is None
    assert next(d for d in scene.devices if d.id == "srv-2").alarm_count is None


def test_a_past_frame_keeps_the_severity_but_drops_category_and_count(monkeypatch):
    _stub(monkeypatch)

    async def values_at(_s, **_kw):
        return {}

    async def severities_at(_s, **_kw):
        return {"srv-1": "MAJOR", "crah-1": "MINOR"}

    monkeypatch.setattr(svc.tele_repo, "values_at", values_at)
    monkeypatch.setattr(svc.tele_repo, "severities_at", severities_at)
    scene = asyncio.run(svc.room_frame(None, "room-1", _NOW - timedelta(hours=3), now=_NOW))
    for r in scene.plan.racks:
        assert r.alarm_category is None and r.alarm_count is None
    for e in scene.plan.equipment:
        assert e.alarm_category is None and e.alarm_count is None
    for d in scene.devices:
        assert d.alarm_category is None and d.alarm_count is None
    assert next(d for d in scene.devices if d.id == "srv-1").max_severity == "MAJOR"
