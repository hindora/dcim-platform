"""Floor plan geometry: room extent and hot/cold aisle classification.

The aisle logic is the part worth pinning. Which way a rack faces decides which
aisle it breathes from, and a mislabelled aisle sends someone looking for a hot
spot on the wrong side of a row.
"""

from __future__ import annotations

from app.services import floorplan as fp


def rack(y: float, facing: str, row: str = "R1", cold: str | None = None,
         hot: str | None = None) -> dict:
    return {"floor_x": 1.0, "floor_y": y, "facing": facing, "row_name": row,
            "cold_aisle": cold, "hot_aisle": hot}


# --- room extent -------------------------------------------------------------

def test_extent_clears_the_furthest_equipment_by_half_a_footprint():
    """The coordinate is a rack CENTRE, so the wall must clear its edge."""
    e = fp.room_extent([(0.3, 1.8), (2.7, 4.2)])
    assert e["width_m"] == round(2.7 + fp.RACK_W / 2 + fp.MARGIN, 2)
    assert e["depth_m"] == round(4.2 + fp.RACK_D / 2 + fp.MARGIN, 2)


def test_extent_is_always_flagged_as_derived():
    """The source has no room dimensions, and the UI must not imply otherwise."""
    assert fp.room_extent([(1.0, 1.0)])["derived"] is True
    assert fp.room_extent([])["derived"] is True


def test_an_empty_room_has_no_extent_rather_than_a_negative_one():
    e = fp.room_extent([])
    assert e["width_m"] == 0.0 and e["depth_m"] == 0.0


# --- aisle classification ----------------------------------------------------

def test_rows_facing_each_other_make_a_cold_aisle():
    """Intakes meet across the gap, so that gap is where the cold air goes.

    The lower row faces higher y and the upper row faces lower y: both are
    looking into the space between them.
    """
    racks = [rack(1.8, "S", "R1", cold="CA1"), rack(4.2, "N", "R2", cold="CA1")]
    aisles = fp.derive_aisles(racks)
    assert len(aisles) == 1
    assert aisles[0].kind == "cold"
    assert aisles[0].label == "CA1"
    assert aisles[0].rows == ["R1", "R2"]


def test_rows_backing_onto_each_other_make_a_hot_aisle():
    racks = [rack(1.8, "N", "R1", hot="HA1"), rack(4.2, "S", "R2", hot="HA1")]
    aisles = fp.derive_aisles(racks)
    assert aisles[0].kind == "hot"
    assert aisles[0].label == "HA1"


def test_rows_facing_the_same_way_are_not_classified():
    """Both rows facing north means one breathes the other's exhaust.

    That is a real (bad) layout, but it is neither a cold nor a hot aisle, and
    labelling it either would be a guess.
    """
    racks = [rack(1.8, "N"), rack(4.2, "N", "R2")]
    assert fp.derive_aisles(racks)[0].kind == "unknown"


def test_a_row_with_mixed_facings_has_no_single_front():
    """Guessing one would put the cold aisle on the wrong side of the row."""
    racks = [rack(1.8, "N"), rack(1.8, "S"), rack(4.2, "N", "R2")]
    assert fp.derive_aisles(racks)[0].kind == "unknown"


def test_the_aisle_spans_the_gap_between_footprints_not_between_centres():
    """A rack is 1.2 m deep; the walkable gap starts at its edge."""
    racks = [rack(1.8, "S"), rack(4.2, "N", "R2")]
    a = fp.derive_aisles(racks)[0]
    assert a.y_start == round(1.8 + fp.RACK_D / 2, 3)   # 2.4
    assert a.y_end == round(4.2 - fp.RACK_D / 2, 3)     # 3.6


def test_rows_that_abut_have_no_aisle_between_them():
    """Row pitch smaller than the rack depth leaves no walkway to draw."""
    racks = [rack(1.8, "S"), rack(2.4, "N", "R2")]
    assert fp.derive_aisles(racks) == []


def test_a_single_row_produces_no_aisles():
    assert fp.derive_aisles([rack(1.8, "N"), rack(1.8, "S")]) == []


def test_three_rows_produce_two_aisles_alternating():
    """The real pattern: cold, hot, cold across a hall."""
    racks = [
        rack(1.8, "S", "R1"), rack(4.2, "N", "R2"),   # face each other -> cold
        rack(6.6, "S", "R3"),                          # R2 backs onto R3 -> hot
    ]
    kinds = [a.kind for a in fp.derive_aisles(racks)]
    assert kinds == ["cold", "hot"]


def test_racks_without_a_position_are_ignored():
    racks = [rack(1.8, "S"), {"floor_y": None, "facing": "N", "row_name": "R9"}]
    assert fp.derive_aisles(racks) == []
