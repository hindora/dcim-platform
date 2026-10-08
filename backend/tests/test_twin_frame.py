"""A room at a moment in the past (docs/27 D8, Phase 4).

The frame is the live scene re-filled from history. These pin which table a
moment reads from, and that the re-fill replaces rather than inherits: a
sensor silent at t leaves no reading, a rack's roll-up is from the frame's
own devices, and alarms are the ones open then.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core import twin_frame as fr
from app.schemas import FloorEquipment, FloorPlan, FloorRack, RoomExtent, TwinDevice

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def test_the_newest_two_hours_read_the_hypertable_then_the_rollups():
    assert fr.choose_source(NOW - timedelta(minutes=30), NOW).table == "telemetry_sample"
    assert fr.choose_source(NOW - timedelta(hours=2), NOW).label == "raw"
    assert fr.choose_source(NOW - timedelta(hours=3), NOW).table == "telemetry_5m"
    assert fr.choose_source(NOW - timedelta(days=2), NOW).label == "5m"
    assert fr.choose_source(NOW - timedelta(days=3), NOW).table == "telemetry_1h"


def test_the_hourly_rollup_gets_a_longer_lookback_than_the_five_minute_one():
    five = fr.choose_source(NOW - timedelta(hours=5), NOW)
    hour = fr.choose_source(NOW - timedelta(days=9), NOW)
    assert five.lookback == timedelta(minutes=15)
    assert hour.lookback == timedelta(hours=3)


def _dev(**kw) -> TwinDevice:
    base = {"id": "d", "name": "d", "device_type": "server", "rack_id": "r", "u_height": 1,
            "status": "ONLINE", "max_severity": "CRITICAL", "power_w": 999.0,
            "inlet_c": 40.0, "temp_c": 40.0, "exhaust_c": 50.0, "rh_pct": 55.0}
    return TwinDevice(**{**base, **kw})


def test_a_frame_replaces_every_live_reading_and_keeps_only_status():
    live = _dev(u_start=5)
    d = fr.device_at(live, {"inlet_temperature": 22.5, "exhaust_temperature": 33.0,
                            "power_draw": 410.0}, None)
    assert (d.inlet_c, d.temp_c, d.exhaust_c, d.power_w) == (22.5, 22.5, 33.0, 410.0)
    # Nothing said about humidity at t: no reading, not the live one.
    assert d.rh_pct is None
    # No alarm open at t: clear, whatever is open now.
    assert d.max_severity == "CLEAR"
    # Communication state has no history and stays as known now.
    assert d.status == "ONLINE"


def test_a_probe_without_an_inlet_takes_its_ambient_reading_as_its_air():
    probe = _dev(device_type="sensor", mount="rack_front")
    d = fr.device_at(probe, {"ambient_temperature": 21.0}, "MINOR")
    assert d.temp_c == 21.0 and d.inlet_c is None and d.max_severity == "MINOR"


def _rack() -> FloorRack:
    return FloorRack(id="r", name="R", x=1.0, y=2.0, facing="N", device_count=3, offline_count=2,
                     load_kw=9.9, max_inlet_c=40.0, max_severity="CRITICAL", free_u=10)


def test_the_racks_rollup_is_from_the_frames_devices():
    devs = [
        fr.device_at(_dev(id="s1", u_start=1), {"inlet_temperature": 21.0}, None),
        fr.device_at(_dev(id="s2", u_start=3), {"inlet_temperature": 24.5}, "MAJOR"),
        fr.device_at(_dev(id="p1", device_type="pdu", mount="zero_u"),
                     {"power_draw": 1500.0}, None),
        fr.device_at(_dev(id="p2", device_type="pdu", mount="zero_u"),
                     {"power_draw": 2500.0}, "WARNING"),
    ]
    r = fr.rack_at(_rack(), devs)
    assert r.load_kw == pytest.approx(4.0)        # the PDUs' meters, not the servers
    assert r.max_inlet_c == 24.5
    assert r.max_severity == "MAJOR"
    assert r.offline_count == 0                   # no history for it; the live 2 does not leak


def test_a_rack_whose_pdus_said_nothing_has_no_load_in_the_frame():
    only = fr.device_at(_dev(id="s1", u_start=1), {"inlet_temperature": 21.0}, None)
    r = fr.rack_at(_rack(), [only])
    assert r.load_kw is None


def test_apply_refills_racks_equipment_and_devices_together():
    plan = FloorPlan(room_id="room", room_name="Hall", extent=RoomExtent(width_m=6.0, depth_m=6.0),
                     racks=[_rack()],
                     equipment=[FloorEquipment(id="c1", name="CRAH", device_type="crah",
                                               status="ONLINE", max_severity="CRITICAL",
                                               supply_c=25.0, return_c=35.0)],
                     unpositioned_equipment=[FloorEquipment(id="c2", name="CRAH 2",
                                                            device_type="crah")])
    devices = [_dev(id="s1", u_start=1), _dev(id="p1", device_type="pdu", mount="zero_u")]
    values = {"s1": {"inlet_temperature": 20.0, "exhaust_temperature": 30.0},
              "p1": {"power_draw": 3000.0},
              "c1": {"supply_air_temp": 18.0, "return_air_temp": 29.0},
              "c2": {"supply_air_temp": 18.5}}
    new_plan, new_devs = fr.apply(plan, devices, values, {"c1": "MINOR"})
    assert new_plan.racks[0].load_kw == pytest.approx(3.0)
    assert new_plan.racks[0].max_inlet_c == 20.0
    assert new_plan.racks[0].max_severity == "CLEAR"
    c1 = new_plan.equipment[0]
    assert (c1.supply_c, c1.return_c, c1.max_severity) == (18.0, 29.0, "MINOR")
    assert new_plan.unpositioned_equipment[0].supply_c == 18.5
    assert new_plan.unpositioned_equipment[0].return_c is None
    assert [d.exhaust_c for d in new_devs] == [30.0, None]
    # Geometry is not telemetry: untouched.
    assert new_plan.extent.width_m == 6.0 and new_plan.racks[0].x == 1.0


def test_severity_ranking_follows_the_alarm_enum():
    assert fr._worst("MINOR", "WARNING") == "MINOR"
    assert fr._worst("CLEAR", "INFO") == "INFO"
    assert fr._worst("CRITICAL", "MAJOR") == "CRITICAL"


# --- how far back a room can be replayed (2026-10-08) -------------------------

def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def _fake_scene():
    from app.schemas import TwinRoomScene
    plan = FloorPlan(room_id="room", room_name="Hall", extent=RoomExtent(width_m=6.0, depth_m=6.0),
                     racks=[_rack()], equipment=[], unpositioned_equipment=[])
    return TwinRoomScene(plan=plan, devices=[_dev(id="s1", u_start=1)])


def test_the_history_range_reads_the_oldest_hour_once_and_caches_it(monkeypatch):
    from app.services import devices as svc
    svc._RANGE_CACHE.clear()
    calls = []
    first = datetime(2026, 9, 1, tzinfo=UTC)

    async def scene(_s, _r):
        return _fake_scene()

    async def earliest(_s, ids):
        calls.append(ids)
        return first

    monkeypatch.setattr(svc, "room_scene", scene)
    monkeypatch.setattr(svc.tele_repo, "earliest_hour", earliest)
    r1 = _run(svc.room_history_range(None, "room", now=NOW))
    r2 = _run(svc.room_history_range(None, "room", now=NOW + timedelta(minutes=5)))
    assert r1.earliest == first and r2.earliest == first
    assert len(calls) == 1 and calls[0] == ["s1"]
    # The horizons are the frame's own source rule, so the picker and the frame agree.
    assert r1.raw_hours == 2.0 and r1.five_min_days == 2.0
    # Stale after ten minutes: asked again.
    _run(svc.room_history_range(None, "room", now=NOW + timedelta(minutes=11)))
    assert len(calls) == 2


def test_a_room_with_no_history_says_so(monkeypatch):
    from app.services import devices as svc
    svc._RANGE_CACHE.clear()

    async def scene(_s, _r):
        return _fake_scene()

    async def earliest(_s, _ids):
        return None

    monkeypatch.setattr(svc, "room_scene", scene)
    monkeypatch.setattr(svc.tele_repo, "earliest_hour", earliest)
    assert _run(svc.room_history_range(None, "room", now=NOW)).earliest is None
