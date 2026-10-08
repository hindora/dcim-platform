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


def _scene(monkeypatch, racks=RACKS, units=UNITS):
    async def building(_s, _dc):
        return SITE

    async def blocks(_s, _dc):
        return racks, units

    monkeypatch.setattr(rack_repo, "site_building", building)
    monkeypatch.setattr(rack_repo, "site_blocks", blocks)
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


def test_the_fabric_and_doors_are_present_and_empty_until_s3(monkeypatch):
    out = _scene(monkeypatch)
    assert out.fabric == [] and out.doors == []
    assert [lv.name for lv in out.levels] == ["G", "1"]
    assert out.model_dump()["rooms"][0]["racks"][0]["x"] == 1.2


def test_a_site_without_blocks_still_draws(monkeypatch):
    out = _scene(monkeypatch, racks=[], units=[])
    assert all(r.racks == [] and r.equipment == [] for r in out.rooms)
