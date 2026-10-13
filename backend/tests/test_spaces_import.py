"""Floorplan 1.2's fabric and compound become space rows (docs/27 §8b/§8c)."""

from __future__ import annotations

import asyncio

from app.importer.simulator import spaces_for
from app.repositories import racks as rack_repo
from app.services import devices as service

FABRIC = {
    "DC1": [
        {
            "id": "DC1/G/corridor",
            "level": "G",
            "kind": "corridor",
            "name": "Corridor",
            "polygon_m": [[0, 15], [22, 15], [22, 17.4], [0, 17.4]],
        },
        {
            "id": "DC1/UPS Room/door",
            "level": "G",
            "kind": "door",
            "name": "UPS Room door",
            "polygon_m": [[-0.1, 2.4], [0.1, 2.4], [0.1, 3.6], [-0.1, 3.6]],
            "attrs": {"room": "DC1/UPS Room", "monitored": True, "external": True},
        },
        {"id": "", "level": "G", "kind": "junk", "polygon_m": [[0, 0]]},  # no id: dropped
    ]
}
COMPOUND = {
    "DC1": {
        "outline_m": [[-18, -10], [47, -10], [47, 35], [-18, 35]],
        "extent_m": {"width": 65.0, "depth": 45.0},
        "zones": [
            {
                "id": "DC1/fence",
                "kind": "fence",
                "name": "Fence",
                "polygon_m": [[-18, -10], [47, -10], [47, 35], [-18, 35]],
            },
            {
                "id": "DC1/Generator Yard",
                "level": "Site",
                "kind": "generator_yard",
                "name": "Generator Yard",
                "polygon_m": [[29, 0], [39, 0], [39, 12.6], [29, 12.6]],
                "attrs": {"room": "DC1/Generator Yard"},
            },
            {
                "id": "DC2/stray",
                "kind": "yard",
                "name": "Other site",
                "polygon_m": [[0, 0], [1, 0], [1, 1]],
                "attrs": {"room": "DC2/Yard"},
            },
        ],
    }
}


def test_fabric_keeps_its_level_and_zones_are_on_site():
    rows = spaces_for("DC1", FABRIC, COMPOUND)
    by = {r["key"]: r for r in rows}
    assert set(by) == {
        "DC1/G/corridor",
        "DC1/UPS Room/door",
        "DC1/fence",
        "DC1/Generator Yard",
        "DC2/stray",
    }
    assert by["DC1/G/corridor"]["level"] == "G" and by["DC1/fence"]["level"] == "Site"
    assert (
        by["DC1/UPS Room/door"]["room"] == "UPS Room"
        and by["DC1/UPS Room/door"]["attrs"]["monitored"] is True
    )
    assert by["DC1/Generator Yard"]["room"] == "Generator Yard"
    # a zone that names another site's room does not get a room here
    assert by["DC2/stray"]["room"] is None
    assert by["DC1/fence"]["polygon_m"][2] == [47.0, 35.0]


def test_a_site_without_the_blocks_yields_nothing():
    assert spaces_for("DC1", {}, {}) == []
    assert spaces_for("DC9", FABRIC, COMPOUND) == []


SITE = {
    "id": "dc1",
    "code": "DC1",
    "name": "DC1",
    "building": {
        "floor_to_floor_m": 5.0,
        "levels": [{"name": "G", "ordinal": 0, "elevation_m": 0}],
        "outline_m": [[0, 0], [25, 0], [25, 21], [0, 21]],
    },
    "compound": {
        "outline_m": [[-18, -10], [47, -10], [47, 35], [-18, 35]],
        "extent_m": {"width": 65.0, "depth": 45.0},
    },
    "rooms": [
        {
            "id": "yard",
            "name": "Generator Yard",
            "level": "Site",
            "room_class": "outdoor",
            "width_m": 10,
            "depth_m": 12.6,
            "origin_x_m": 29,
            "origin_y_m": 0,
            "level_elevation_m": None,
            "rack_count": 0,
            "device_count": 5,
            "max_severity": "CLEAR",
        }
    ],
    "spaces": [
        {
            "external_key": "DC1/G/corridor",
            "level": "G",
            "kind": "corridor",
            "name": "Corridor",
            "polygon_m": [[0, 15], [22, 15], [22, 17.4], [0, 17.4]],
            "room_id": None,
            "attributes": {},
        },
        {
            "external_key": "DC1/Generator Yard",
            "level": "Site",
            "kind": "generator_yard",
            "name": "Generator Yard",
            "polygon_m": [[29, 0], [39, 0], [39, 12.6], [29, 12.6]],
            "room_id": "yard",
            "attributes": {"room": "DC1/Generator Yard"},
        },
    ],
}


def test_the_site_scene_splits_fabric_from_the_compound(monkeypatch):
    async def building(_s, _dc):
        return SITE

    async def blocks(_s, _dc):
        return [], []

    async def doors(_s, _dc):
        return []

    async def capacity(_s, _dc):
        return {}

    monkeypatch.setattr(rack_repo, "site_building", building)
    monkeypatch.setattr(rack_repo, "site_blocks", blocks)
    monkeypatch.setattr(rack_repo, "site_doors", doors)
    monkeypatch.setattr(rack_repo, "site_room_capacity", capacity)
    out = asyncio.run(service.site_scene(None, "dc1"))
    assert [f.kind for f in out.fabric] == ["corridor"] and out.fabric[0].level == "G"
    assert out.compound is not None and out.compound.extent_m == {"width": 65.0, "depth": 45.0}
    assert [z.kind for z in out.compound.zones] == ["generator_yard"]
    assert out.compound.zones[0].room_id == "yard"
    assert out.rooms[0].room_class == "outdoor" and out.rooms[0].level == "Site"
