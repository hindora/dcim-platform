"""The site scene carries each room's racks and plant as blocks, so the
building rung (docs/27 §8b) draws a whole site from one call."""

from __future__ import annotations

import asyncio

from app.repositories import racks as rack_repo
from app.services import devices as service

SITE = {
    "id": "dc1",
    "code": "DC1",
    "name": "DC1 Chicago",
    "building": {
        "floor_to_floor_m": 5.0,
        "outline_m": [[0, 0], [31, 0], [31, 15], [0, 15]],
        "levels": [
            {"name": "G", "ordinal": 0, "elevation_m": 0},
            {"name": "1", "ordinal": 1, "elevation_m": 5},
        ],
    },
    "rooms": [
        {
            "id": "hall",
            "name": "Server Hall A",
            "level": "1",
            "room_class": "white_space",
            "width_m": 12.2,
            "depth_m": 12.3,
            "origin_x_m": 0,
            "origin_y_m": 0,
            "level_elevation_m": 5,
            "rack_count": 2,
            "device_count": 40,
            "max_severity": "MAJOR",
        },
        {
            "id": "ups",
            "name": "UPS Room",
            "level": "G",
            "room_class": "facility",
            "width_m": 9,
            "depth_m": 6,
            "origin_x_m": 0,
            "origin_y_m": 0,
            "level_elevation_m": 0,
            "rack_count": 0,
            "device_count": 8,
            "max_severity": "CLEAR",
        },
    ],
}
RACKS = [
    {
        "id": "r1",
        "name": "R1-01",
        "room_id": "hall",
        "floor_x": 1.2,
        "floor_y": 2.0,
        "width_m": 0.6,
        "depth_m": 1.2,
        "max_severity": "MAJOR",
    },
    {
        "id": "r2",
        "name": "R1-02",
        "room_id": "hall",
        "floor_x": 1.8,
        "floor_y": 2.0,
        "width_m": None,
        "depth_m": None,
        "max_severity": "CLEAR",
    },
]
UNITS = [
    {
        "id": "u1",
        "name": "UPS-A",
        "device_type": "ups",
        "room_id": "ups",
        "floor_x": 1.5,
        "floor_y": 1.0,
        "footprint_w_m": 2.6,
        "footprint_d_m": 0.9,
        "height_m": 2.0,
        "rotation_deg": 90,
        "max_severity": "CLEAR",
    },
]


# One contact, on the hall's door, reading closed with nothing open against it.
DOORS = [{"id": "door-ha", "room_id": "hall", "level": "1", "floor_x": 0.3, "floor_y": 12.2,
          "rotation_deg": 180, "open": False, "at": "2026-10-13T09:00:00+00:00",
          "max_severity": "CLEAR"}]


# The hall's two racks, summed: 30 of 44 kW metered, 28 allocated and 4 held,
# 61 of 84 U filled. The UPS room has no racks and so no row.
CAPACITY = {"hall": {"room_id": "hall", "load_kw": 30.0, "rated_kw": 44.0, "allocated_kw": 28.0,
                     "reserved_kw": 4.0, "rated_load_kw": 30.0, "unrated_racks": 0,
                     "u_used": 61, "u_total": 84}}


def _scene(monkeypatch, racks=RACKS, units=UNITS, capacity=CAPACITY, fire=(), marks=()):
    async def building(_s, _dc):
        return SITE

    async def blocks(_s, _dc):
        return racks, units

    async def doors(_s, _dc):
        return DOORS

    monkeypatch.setattr(rack_repo, "site_building", building)
    monkeypatch.setattr(rack_repo, "site_blocks", blocks)
    async def room_capacity(_s, _dc):
        return capacity

    monkeypatch.setattr(rack_repo, "site_doors", doors)
    async def fire_read(_s, _dc):
        return list(fire)

    monkeypatch.setattr(rack_repo, "site_room_capacity", room_capacity)
    monkeypatch.setattr(rack_repo, "site_fire", fire_read)

    async def marks_read(_s, _dc):
        return list(marks)

    monkeypatch.setattr(rack_repo, "site_alarm_marks", marks_read)
    return asyncio.run(service.site_scene(None, "dc1"))


def test_racks_and_plant_land_in_their_rooms(monkeypatch):
    out = _scene(monkeypatch)
    hall = next(r for r in out.rooms if r.id == "hall")
    ups = next(r for r in out.rooms if r.id == "ups")
    assert [r.name for r in hall.racks] == ["R1-01", "R1-02"] and hall.equipment == []
    assert (
        hall.racks[0].w_m == 0.6 and hall.racks[1].w_m is None
    )  # the viewer falls back to 0.6 x 1.2
    assert hall.racks[0].max_severity == "MAJOR"
    assert [u.name for u in ups.equipment] == ["UPS-A"] and ups.racks == []
    assert ups.equipment[0].facing_deg == 90 and ups.equipment[0].h_m == 2.0


def test_the_fabric_is_empty_without_spaces_and_the_doors_carry_their_contacts(monkeypatch):
    out = _scene(monkeypatch)
    assert out.fabric == []
    assert [(d.device_id, d.room_id, d.open, d.max_severity) for d in out.doors] == [
        ("door-ha", "hall", False, "CLEAR")]
    assert [lv.name for lv in out.levels] == ["G", "1"]
    assert out.model_dump()["rooms"][0]["racks"][0]["x"] == 1.2


def test_a_site_without_blocks_still_draws(monkeypatch):
    out = _scene(monkeypatch, racks=[], units=[])
    assert all(r.racks == [] and r.equipment == [] for r in out.rooms)


def test_a_room_carries_the_sum_of_its_racks_and_a_plant_room_carries_nothing(monkeypatch):
    out = _scene(monkeypatch)
    hall = next(r for r in out.rooms if r.id == "hall")
    ups = next(r for r in out.rooms if r.id == "ups")
    assert (hall.power.load_kw, hall.power.rated_kw) == (30.0, 44.0)
    assert (hall.power.allocated_kw, hall.power.reserved_kw) == (28.0, 4.0)
    assert (hall.space.u_used, hall.space.u_total) == (61, 84)
    assert ups.power is None and ups.space is None


def test_an_unmetered_unrated_room_says_so_rather_than_reading_zero(monkeypatch):
    bare = {"hall": {"room_id": "hall", "load_kw": None, "rated_kw": None, "allocated_kw": None,
                     "reserved_kw": None, "u_used": 0, "u_total": 84}}
    hall = next(r for r in _scene(monkeypatch, capacity=bare).rooms if r.id == "hall")
    assert hall.power.load_kw is None and hall.power.rated_kw is None
    assert hall.space.u_used == 0 and hall.space.u_total == 84


def test_a_room_with_an_unrated_rack_divides_only_the_rated_racks_load(monkeypatch):
    # Three racks metering 36 kW, two of them rated 22 kW each and carrying 30.
    mixed = {"hall": {"room_id": "hall", "load_kw": 36.0, "rated_kw": 44.0, "allocated_kw": 28.0,
                      "reserved_kw": None, "rated_load_kw": 30.0, "unrated_racks": 1,
                      "u_used": 70, "u_total": 126}}
    hall = next(r for r in _scene(monkeypatch, capacity=mixed).rooms if r.id == "hall")
    assert hall.power.load_kw == 36.0 and hall.power.rated_load_kw == 30.0
    assert hall.power.unrated_racks == 1


def test_a_site_with_no_fire_instrument_says_nothing_about_fire(monkeypatch):
    assert _scene(monkeypatch).fire is None


def test_fire_folds_into_three_panel_states_and_one_entry_per_room(monkeypatch):
    rows = [
        {"id": "facp", "name": "FACP-DC1-OR", "room_id": "ops", "fire_alarm": True,
         "fire_trouble": False, "fire_supervisory": True},
        {"id": "asd", "name": "ASD-DC1-HA", "room_id": "hall", "smoke_stage": 4.0,
         "smoke_obscuration": 4.8},
        {"id": "gsp", "name": "GSP-DC1-HA", "room_id": "hall", "gas_state": 2.0,
         "gas_countdown_s": 21.0, "agent_kpa": 2500.0, "agent_released": False},
    ]
    fire = _scene(monkeypatch, fire=rows).fire
    assert (fire.alarm, fire.trouble, fire.supervisory) == (True, False, True)
    (hall,) = fire.rooms                      # the panel's own room has no detector, so no entry
    assert hall.room_id == "hall" and hall.smoke_stage == 4 and hall.gas_state == 2
    assert hall.gas_countdown_s == 21.0 and hall.agent_kpa == 2500.0
    assert hall.agent_released is False


def test_a_room_with_a_detector_and_no_release_panel_reports_only_smoke(monkeypatch):
    rows = [{"id": "asd", "name": "ASD-DC1-HA", "room_id": "hall", "smoke_stage": 0.0,
             "smoke_obscuration": 0.009}]
    fire = _scene(monkeypatch, fire=rows).fire
    assert fire.alarm is None                 # no panel reports, so nothing is claimed for it
    assert fire.rooms[0].smoke_stage == 0 and fire.rooms[0].gas_state is None
    assert fire.rooms[0].agent_released is None


def test_a_rack_unit_and_room_carry_their_worst_alarms_category(monkeypatch):
    marks = [
        # two on a server in rack r1: the critical power alarm outranks the minor IT one
        {"device_id": "srv1", "rack_id": "r1", "room_id": "hall", "category": "it_equipment",
         "severity": "MINOR", "last_seen": "2026-10-15T10:00:00Z"},
        {"device_id": "pdu1", "rack_id": "r1", "room_id": "hall", "category": "power",
         "severity": "CRITICAL", "last_seen": "2026-10-15T09:00:00Z"},
        # a wall sensor in the UPS room: no rack, the room carries it
        {"device_id": "thr1", "rack_id": None, "room_id": "ups", "category": "environmental",
         "severity": "MAJOR", "last_seen": "2026-10-15T10:00:00Z"},
    ]
    out = _scene(monkeypatch, marks=marks)
    hall = next(r for r in out.rooms if r.id == "hall")
    ups = next(r for r in out.rooms if r.id == "ups")
    r1 = next(k for k in hall.racks if k.id == "r1")
    assert (r1.alarm_category, r1.alarm_count) == ("power", 2)
    assert (hall.alarm_category, hall.alarm_count) == ("power", 2)
    assert (ups.alarm_category, ups.alarm_count) == ("environmental", 1)
    clear = [k for k in hall.racks if k.id != "r1"]
    assert all(k.alarm_category is None and k.alarm_count == 0 for k in clear)


def test_alarm_marks_breaks_a_severity_tie_on_the_latest():
    rows = [
        {"device_id": "a", "rack_id": "r", "room_id": "m", "category": "cooling",
         "severity": "MAJOR", "last_seen": "2026-10-15T09:00:00Z"},
        {"device_id": "b", "rack_id": "r", "room_id": "m", "category": "network",
         "severity": "MAJOR", "last_seen": "2026-10-15T10:00:00Z"},
    ]
    out = service.alarm_marks(rows)
    assert out[("rack", "r")] == {"alarm_category": "network", "alarm_count": 2}
    assert out[("device", "a")] == {"alarm_category": "cooling", "alarm_count": 1}
