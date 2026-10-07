"""Physical geometry import and the floor plan built on it (docs/27 Phase 0).

The floor plan used to INVENT its geometry: the outline was the racks' bounding
box plus a margin, aisles were guessed from rack facing, and every CRAH and
chiller was listed under the drawing for want of a coordinate. The simulator now
exports the geometry (its Phase S1) and migration 0099 stores it. These pin the
rules that decide what the stored geometry means.
"""

from __future__ import annotations

from pathlib import Path

from app.importer import simulator as imp
from app.services import floorplan as fp

SRC = (Path(__file__).resolve().parents[1] / "app" / "importer" / "simulator.py").read_text(
    encoding="utf-8")


# --- what counts as rack-mounted -------------------------------------------

def test_the_exported_mount_decides_rack_membership():
    # A wall T/RH probe used to be filed under its rack_row/rack_num and made a
    # 'rack' in the generator room. The export now says how it is held.
    assert imp._is_rack_mounted({"device_type": "sensor", "mount": "wall"}) is False
    assert imp._is_rack_mounted({"device_type": "sensor", "mount": "pipe"}) is False
    assert imp._is_rack_mounted({"device_type": "sensor", "mount": "rack_front"}) is True
    assert imp._is_rack_mounted({"device_type": "pdu", "mount": "zero_u"}) is True


def test_an_export_without_mount_falls_back_to_the_type_list():
    assert imp._is_rack_mounted({"device_type": "chiller"}) is False
    assert imp._is_rack_mounted({"device_type": "server"}) is True


def test_u_height_comes_from_the_export_before_the_model_name_guess():
    assert imp._u_height({"u_height": 2, "model_name": "Dell R640"}) == 2
    # predates the field: the old guess still answers
    assert imp._u_height({"model_name": "Thing 4U", "device_type": "server"}) == 4


def test_facing_is_kept_for_free_standing_units_only():
    chiller = {"device_type": "chiller", "mount": "floor", "facing_deg": 180.0,
               "footprint_m": {"width": 2.55, "depth": 4.72, "height": 2.82,
                               "basis": "datasheet"}, "mount_height_m": 1.41}
    g = imp._geometry(chiller)
    assert (g["rot"], g["fw"], g["fd"], g["fh"], g["fbasis"]) == \
        (180.0, 2.55, 4.72, 2.82, "datasheet")
    # a server faces the way its rack does; a copy per server would drift
    server = {"device_type": "server", "mount": "rack", "facing_deg": 0.0}
    assert imp._geometry(server)["rot"] is None


# --- re-import never undoes a hand correction --------------------------------

def test_manual_geometry_survives_a_re_import():
    for table, col in (("room", "origin_x_m"), ("room", "width_m"),
                       ("rack", "floor_x"), ("device", "floor_x"), ("device", "mount")):
        assert f"WHEN {table}.geometry_source = 'manual'" in SRC, (table, col)
        assert f"THEN {table}.{col}" in SRC, (table, col)


def test_pruning_never_touches_white_space():
    start = SRC.index("async def _prune_plant_racks")
    body = SRC[start:SRC.index("return len(rows)", start)]
    assert "rm.room_class = 'facility'" in body
    assert "capacity_reservation" in body          # a reserved rack is never pruned
    assert "NOT EXISTS (SELECT 1 FROM device d WHERE d.rack_id = r.id)" in body


# --- stored geometry beats derivation ----------------------------------------

def test_stored_extent_is_not_flagged_derived():
    assert fp.stored_extent(16.0, 15.0) == {"width_m": 16.0, "depth_m": 15.0,
                                            "derived": False}
    assert fp.stored_extent(None, 15.0) is None
    assert fp.stored_extent(0, 15.0) is None


def test_stored_aisles_become_bands_around_their_centre_line():
    aisles = fp.stored_aisles([
        {"name": "CA1", "kind": "cold", "y_m": 3.0, "width_m": 1.2,
         "between_rows": [1, 2], "contained": True},
        {"name": "HA0", "kind": "hot", "y_m": 0.6, "width_m": 1.2,
         "between_rows": [0, 1], "contained": False},
    ])
    ca = aisles[0]
    assert (ca.y_start, ca.y_end, ca.kind, ca.label, ca.contained) == \
        (2.4, 3.6, "cold", "CA1", True)
    assert ca.rows == ["R1", "R2"]
    # row 0 is the wall side of a perimeter aisle, not a row
    assert aisles[1].rows == ["R1"]


def test_a_derived_aisle_does_not_claim_to_know_containment():
    racks = [{"floor_x": 1.0, "floor_y": 1.8, "facing": "S", "row_name": "R1"},
             {"floor_x": 1.0, "floor_y": 4.2, "facing": "N", "row_name": "R2"}]
    assert fp.derive_aisles(racks)[0].contained is None


def test_geometry_only_import_leaves_endpoints_collectors_and_lifecycle_alone():
    """The full run rewrites each endpoint's collector shard from --collector-id
    and so CLEARS it when that is not passed; on a live, sharded estate that
    hands every endpoint to every collector. The geometry refresh must not."""
    start = SRC.index("async def run_geometry(")
    body = SRC[start:SRC.index("async def _upsert_device(", start)]
    for forbidden in ("device_endpoint", "_upsert_endpoints", "_upsert_connection",
                      "_decommission_missing", "_resurrect", "collector_id", "lifecycle ="):
        assert forbidden not in body, forbidden
    assert "geometry_source IS DISTINCT FROM 'manual'" in body


def test_the_3d_scene_draws_only_live_devices_in_this_rooms_racks():
    """The 3D room lists what is in each rack; a decommissioned box left on a
    rack_id must not be drawn as if it were still there."""
    repo = (Path(__file__).resolve().parents[1] / "app" / "repositories"
            / "racks.py").read_text(encoding="utf-8")
    start = repo.index("async def room_rack_devices(")
    body = repo[start:repo.index("return [dict(r) for r in rows]", start)]
    assert "d.lifecycle <> 'decommissioned'" in body
    assert "rr.room_id = CAST(:room_id AS uuid)" in body
    assert "d.mount_height_m" in body          # door probes have no U to place them by


def test_rack_load_is_metered_at_the_pdus_not_summed_twice():
    """A rack PDU's reading already contains every server plugged into it.
    Summing all devices counted each server twice - R2-01 read 32.5 kW for a
    16.5 kW rack, and the power overlay painted healthy racks red."""
    from app.repositories.racks import _RACK_LOAD_W, _RACK_SUMMARY
    assert "d.device_type IN ('pdu', 'floor_pdu')" in _RACK_LOAD_W
    assert "d.device_type NOT IN ('pdu', 'floor_pdu')" in _RACK_LOAD_W
    assert "COALESCE(sum(ds.power_w), 0)" not in _RACK_SUMMARY
