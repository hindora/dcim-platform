"""The room builder's rules (docs/27 Phase 6): a footprint stands inside its
room and on nothing else, a hand-placed object is marked so no import moves it,
and the drawing is edited by operators only."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.v1 import infrastructure as api
from app.core import placement as rules
from app.services import placement as service

BACKEND = Path(__file__).resolve().parents[1]
ROUTER = (BACKEND / "app" / "api" / "v1" / "infrastructure.py").read_text(encoding="utf-8")
IMPORTER = (BACKEND / "app" / "importer" / "simulator.py").read_text(encoding="utf-8")


def test_racks_in_a_row_touch_without_overlapping():
    a = rules.footprint(1.2, 2.0, 0.6, 1.2, name="R1-01")
    b = rules.footprint(1.8, 2.0, 0.6, 1.2, name="R1-02")
    assert not a.overlaps(b) and not b.overlaps(a)
    assert rules.problems(a, 12.2, 12.3, [b]) == []


def test_a_rack_set_down_on_its_neighbour_is_refused_by_name():
    moved = rules.footprint(1.7, 2.0, 0.6, 1.2, name="R1-01")
    others = [rules.footprint(1.8, 2.0, 0.6, 1.2, name="R1-02"),
              rules.footprint(6.0, 6.0, 0.6, 1.2, name="R3-01")]
    assert rules.problems(moved, 12.2, 12.3, others) == ["overlaps R1-02"]


def test_a_footprint_through_the_wall_is_refused():
    out = rules.problems(rules.footprint(0.2, 2.0, 0.6, 1.2), 12.2, 12.3, [])
    assert out == ["outside the room (12.2 x 12.3 m)"]
    # Flush against the wall is inside.
    assert rules.problems(rules.footprint(0.3, 0.6, 0.6, 1.2), 12.2, 12.3, []) == []


def test_a_room_with_no_stored_size_has_no_wall_to_be_outside_of():
    assert rules.problems(rules.footprint(-3.0, 2.0, 0.6, 1.2), None, None, []) == []


def test_a_quarter_turn_swaps_the_footprint_and_any_other_angle_bounds_it():
    flat = rules.footprint(5, 5, 2.6, 0.9, 0)
    turned = rules.footprint(5, 5, 2.6, 0.9, 90)
    assert round(flat.x1 - flat.x0, 6) == 2.6 and round(flat.y1 - flat.y0, 6) == 0.9
    assert round(turned.x1 - turned.x0, 6) == 0.9 and round(turned.y1 - turned.y0, 6) == 2.6
    skew = rules.footprint(5, 5, 2.6, 0.9, 45)
    assert skew.x1 - skew.x0 > 2.4 and skew.y1 - skew.y0 > 2.4
    # A half turn is the same footprint.
    assert rules.footprint(5, 5, 2.6, 0.9, 180) == flat


def test_the_snap_is_the_raised_floor_tile():
    assert rules.snap(1.37) == 1.2 and rules.snap(1.55) == 1.8 and rules.snap(0.29) == 0.0


def test_many_clashes_are_named_up_to_four():
    moved = rules.footprint(3, 3, 6, 6, name="big")
    others = [rules.footprint(1 + i, 3, 0.6, 1.2, name=f"R{i}") for i in range(6)]
    assert rules.problems(moved, 20, 20, others) == ["overlaps R0, R1, R2, R3 and 2 more"]


# --- the service, over a session that answers the three reads it makes ------

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _Session:
    """Answers by what the statement reads; records what it writes."""

    def __init__(self, *, rack=None, device=None, racks=(), units=()):
        self.rack, self.device, self.racks, self.units = rack, device, list(racks), list(units)
        self.writes: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        if sql.startswith("UPDATE"):
            self.writes.append((sql, params))
            return _Result([])
        if "FROM rack r JOIN rack_row rr ON rr.id = r.row_id JOIN room rm" in sql:
            return _Result([self.rack] if self.rack else [])
        if "FROM device d JOIN room rm" in sql:
            return _Result([self.device] if self.device else [])
        if "FROM rack r JOIN rack_row rr" in sql:
            return _Result(self.racks)
        if "FROM device d" in sql:
            return _Result(self.units)
        if sql.startswith("SELECT geometry_source FROM"):
            row = self.rack if "FROM rack" in sql else self.device
            return _Result([row] if row else [])
        raise AssertionError(sql)


RACK = {"id": "r1", "name": "R1-01", "floor_x": 1.2, "floor_y": 2.0, "width_m": 0.6,
        "depth_m": 1.2, "geometry_source": "import", "room_id": "hall", "room_w": 12.2,
        "room_d": 12.3}
NEIGHBOURS = [
    {"id": "r1", "name": "R1-01", "floor_x": 1.2, "floor_y": 2.0, "width_m": 0.6, "depth_m": 1.2},
    {"id": "r2", "name": "R1-02", "floor_x": 1.8, "floor_y": 2.0, "width_m": None,
     "depth_m": None},
]
CRAH = {"id": "c1", "name": "CRAH-HA-01", "floor_x": 6.0, "floor_y": 0.5, "footprint_w_m": 1.75,
        "footprint_d_m": 0.9, "rotation_deg": 0}


def test_a_moved_rack_is_marked_manual_and_does_not_collide_with_itself():
    s = _Session(rack=RACK, racks=NEIGHBOURS, units=[CRAH])
    before, after = asyncio.run(service.move_rack(s, "r1", x=1.2, y=5.004))
    assert before == {"x": 1.2, "y": 2.0, "geometry_source": "import"}
    assert after == {"x": 1.2, "y": 5.0, "geometry_source": "manual"}
    (sql, params), = s.writes
    assert "geometry_source = 'manual'" in sql and params["y"] == 5.0


def test_a_rack_moved_onto_a_neighbour_or_a_crah_writes_nothing():
    s = _Session(rack=RACK, racks=NEIGHBOURS, units=[CRAH])
    with pytest.raises(service.PlacementRefusedError) as onto_rack:
        asyncio.run(service.move_rack(s, "r1", x=1.7, y=2.0))
    assert onto_rack.value.problems == ["overlaps R1-02"]
    with pytest.raises(service.PlacementRefusedError) as onto_crah:
        asyncio.run(service.move_rack(s, "r1", x=6.0, y=0.8))
    assert onto_crah.value.problems == ["overlaps CRAH-HA-01"]
    assert s.writes == []


def test_an_unknown_rack_is_not_found():
    with pytest.raises(service.PlacementNotFoundError):
        asyncio.run(service.move_rack(_Session(), "nope", x=1, y=1))


UNIT = {"id": "c1", "name": "CRAH-HA-01", "floor_x": 6.0, "floor_y": 0.5, "rotation_deg": 0,
        "mount": "floor", "rack_id": None, "footprint_w_m": 1.75, "footprint_d_m": 0.9,
        "geometry_source": "import", "room_id": "hall", "room_w": 12.2, "room_d": 12.3}


def test_a_unit_keeps_its_facing_unless_it_is_turned():
    s = _Session(device=UNIT, racks=NEIGHBOURS, units=[CRAH])
    _, kept = asyncio.run(service.move_unit(s, "c1", x=8.0, y=0.5, rotation_deg=None))
    assert kept == {"x": 8.0, "y": 0.5, "rotation_deg": 0.0, "geometry_source": "manual"}
    _, turned = asyncio.run(service.move_unit(s, "c1", x=8.0, y=1.0, rotation_deg=90))
    assert turned["rotation_deg"] == 90


def test_a_turn_that_puts_the_unit_through_the_wall_is_refused():
    s = _Session(device=UNIT, racks=[], units=[])
    # 1.75 m long, turned to stand along y, centred 0.5 m from the wall.
    with pytest.raises(service.PlacementRefusedError):
        asyncio.run(service.move_unit(s, "c1", x=8.0, y=0.5, rotation_deg=90))


@pytest.mark.parametrize("change", [{"rack_id": "r1"}, {"mount": "wall"},
                                    {"footprint_w_m": None}])
def test_only_a_floor_standing_unit_with_a_footprint_can_be_placed(change):
    s = _Session(device={**UNIT, **change})
    with pytest.raises(service.PlacementNotFoundError):
        asyncio.run(service.move_unit(s, "c1", x=8.0, y=0.5, rotation_deg=None))


def test_releasing_hands_the_object_back_to_the_import():
    s = _Session(rack={**RACK, "geometry_source": "manual"})
    assert asyncio.run(service.release(s, "rack", "r1")) == "manual"
    (sql, _), = s.writes
    assert "geometry_source = 'import'" in sql
    assert asyncio.run(service.release(_Session(), "device", "nope")) is None


def test_a_placement_off_the_drawing_is_refused_before_any_read():
    with pytest.raises(ValidationError):
        api.Placement(x=-0.1, y=1)
    with pytest.raises(ValidationError):
        api.Placement(x=1, y=1, rotation_deg=360)
    assert api.Placement(x=1.2, y=2.0).rotation_deg is None


def test_the_drawing_is_edited_by_operators_and_every_edit_is_audited():
    for fn in ("async def place_rack(", "async def place_unit(", "async def release_placement("):
        body = ROUTER[ROUTER.index(fn):]
        body = body[:body.index("\n\n\n")]
        assert 'require_role("operator")' in body, fn
    assert ROUTER.count('action="geometry.place"') == 1
    assert ROUTER.count('action="geometry.release"') == 1


def test_an_import_leaves_a_hand_placed_rack_and_unit_where_they_stand():
    assert "floor_x = CASE WHEN rack.geometry_source = 'manual' THEN rack.floor_x" in IMPORTER
    assert "floor_x = CASE WHEN device.geometry_source = 'manual'" in IMPORTER
