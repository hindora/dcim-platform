"""The thermal indices and the per-aisle heat field (docs/27 Phase 2).

RCI, RTI and SHI are published numbers with literature definitions, so the
arithmetic is pinned against hand-worked cases. The field's one rule that
matters - an exhaust reading never informs a cold-aisle cell - is pinned
with a room where breaking it would be visible.
"""

from __future__ import annotations

import pytest

from app.core import thermal_field as tf
from app.core import thermal_indices as ti
from app.core.ashrae import envelope_for
from app.schemas import FloorEquipment, FloorPlan, FloorRack, RoomExtent, TwinDevice
from app.services import devices as svc

A1 = envelope_for("A1")


# --- RCI (Herrlin) -----------------------------------------------------------

def test_rci_is_100_when_every_intake_sits_in_the_recommended_band():
    assert ti.rci([18.0, 22.5, 27.0], A1) == (100.0, 100.0)


def test_rci_hi_falls_by_the_over_temperature_share_of_the_allowable_span():
    # Four intakes, one of them 2.5 K over 27 C. A1's span above recommended
    # is 32 - 27 = 5 K, so the loss is 2.5 / (5 * 4) = 12.5 %.
    hi, lo = ti.rci([24.0, 25.0, 26.0, 29.5], A1)
    assert hi == pytest.approx(87.5)
    assert lo == 100.0
    assert ti.rci_rating(hi) == "poor"


def test_rci_lo_uses_the_floor_of_the_band_and_the_lower_allowable_limit():
    # One of two intakes 1.5 K under 18 C against a 3 K span: 1.5 / 6 = 25 %.
    hi, lo = ti.rci([20.0, 16.5], A1)
    assert (hi, lo) == (100.0, 75.0)


def test_rci_never_goes_below_zero():
    hi, _ = ti.rci([45.0, 45.0], A1)
    assert hi == 0.0


def test_rci_without_intakes_is_absent_not_perfect():
    assert ti.rci([], A1) == (None, None)


def test_rci_rating_bands_are_herrlins():
    assert ti.rci_rating(96.0) == "good"
    assert ti.rci_rating(95.9) == "acceptable"
    assert ti.rci_rating(91.0) == "acceptable"
    assert ti.rci_rating(90.9) == "poor"
    assert ti.rci_rating(None) is None


# --- RTI (Herrlin) -----------------------------------------------------------

def test_equipment_rise_is_the_power_weighted_mean_sum_p_over_sum_p_by_dt():
    # Two servers moving air: 1 kW at 10 K and 3 kW at 20 K. Airflow goes as
    # P/dT, so the airflow-weighted dT is (1+3) / (1/10 + 3/20) = 16 K - not
    # the plain 15 K mean.
    dt, unweighted = ti.power_weighted_dt([(10.0, 1000.0), (20.0, 3000.0)])
    assert dt == pytest.approx(16.0)
    assert unweighted == 0


def test_equipment_rise_falls_back_to_a_flagged_plain_mean_without_power():
    dt, unweighted = ti.power_weighted_dt([(10.0, None), (20.0, 3000.0)])
    assert dt == pytest.approx(15.0)
    assert unweighted == 1


def test_a_non_positive_rise_is_a_sensor_fault_and_is_dropped():
    assert ti.power_weighted_dt([(-2.0, 500.0), (0.0, 500.0)]) == (None, 0)


def test_rti_reads_100_when_balanced_and_above_it_under_recirculation():
    assert ti.rti(18.0, 30.0, 12.0) == pytest.approx(100.0)
    # The return is warmer than the kit's rise alone explains: exhaust is
    # finding its way back round into intakes.
    assert ti.rti(18.0, 33.0, 12.0) == pytest.approx(125.0)
    # Cooler: supply is bypassing the kit straight back to the unit.
    assert ti.rti(18.0, 27.0, 12.0) == pytest.approx(75.0)
    assert ti.rti(None, 30.0, 12.0) is None
    assert ti.rti(18.0, 30.0, 0.0) is None


# --- SHI / RHI (Sharma) ------------------------------------------------------

def test_shi_is_the_pre_entry_share_of_the_heat_picked_up():
    # Supply 18, inlet 21, exhaust 33: 3 of the 15 K rise happened before the
    # rack - a fifth recirculated.
    assert ti.shi([(21.0, 33.0)], 18.0) == pytest.approx(0.2)


def test_shi_is_zero_when_the_intake_breathes_pure_supply_and_clamped_below_it():
    assert ti.shi([(18.0, 30.0)], 18.0) == 0.0
    assert ti.shi([(17.5, 30.0)], 18.0) == 0.0
    assert ti.shi([(21.0, 33.0)], None) is None
    assert ti.shi([], 18.0) is None


# --- the room roll-up --------------------------------------------------------

def _rack(rack_id, inlets, exhausts, power=1000.0):
    r = ti.RackThermal(rack_id=rack_id, inlets_c=list(inlets), exhausts_c=list(exhausts))
    for i, e in zip(inlets, exhausts, strict=True):
        r.device_rises.append((e - i, power))
    return r


def test_room_indices_carry_their_evidence_counts():
    racks = [_rack("a", [22.0, 23.0], [34.0, 35.0]), _rack("b", [29.0], [40.0])]
    r = ti.room_indices(racks, A1, [(18.0, 30.0), (18.0, None), (None, None)])
    assert r.intakes == 3 and r.exhausts == 3
    assert r.units_in_ref == 1        # the two half-read units are left out
    assert r.supply_ref_c == 18.0 and r.return_ref_c == 30.0
    assert r.hot_spots == 1           # rack b at 29 C
    b = next(x for x in r.racks if x.rack_id == "b")
    assert b.hot == "allowable"
    assert b.spread_k == 0.0 and b.rise_k == pytest.approx(11.0)
    a = next(x for x in r.racks if x.rack_id == "a")
    assert a.spread_k == pytest.approx(1.0)
    assert a.shi == pytest.approx((22.5 - 18) / (34.5 - 18))
    assert a.rhi == pytest.approx(1 - a.shi)


def test_a_rack_past_the_allowable_ceiling_is_an_out_hot_spot():
    r = ti.rack_index(_rack("x", [33.0], [45.0]), A1, 18.0)
    assert r.hot == "out"
    r = ti.rack_index(_rack("y", [26.9], [40.0]), A1, 18.0)
    assert r.hot is None


# --- attribution: which reading is an intake, which an exhaust ----------------

def _dev(**kw) -> TwinDevice:
    base = {"id": "d", "name": "d", "device_type": "server", "rack_id": "r", "u_height": 1}
    return TwinDevice(**{**base, **kw})


def test_a_server_gives_its_inlet_and_its_exhaust():
    assert svc.device_side_readings(_dev(u_start=10, inlet_c=22.0, exhaust_c=34.0)) == (22.0, 34.0)


def test_rack_probes_go_to_the_face_they_hang_on():
    front = _dev(device_type="sensor", mount="rack_front", temp_c=21.0)
    rear = _dev(device_type="sensor", mount="rack_rear", temp_c=33.0)
    assert svc.device_side_readings(front) == (21.0, None)
    assert svc.device_side_readings(rear) == (None, 33.0)


def test_a_rack_pdus_ambient_probe_informs_neither_index():
    pdu = _dev(device_type="pdu", mount="zero_u", temp_c=28.0)
    assert svc.device_side_readings(pdu) == (None, None)
    assert svc.device_side_readings(_dev(device_type="pdu", u_start=0, temp_c=28.0)) == (None, None)


def test_sensor_height_comes_from_the_u_or_the_mount():
    assert svc.device_height_m(_dev(u_start=1, u_height=1)) == pytest.approx(0.1 + 0.5 * 0.04445)
    assert svc.device_height_m(_dev(u_start=20, u_height=2)) == pytest.approx(0.1 + 20 * 0.04445)
    assert svc.device_height_m(_dev(mount="rack_front", mount_height_m=1.5)) == 1.5
    assert svc.device_height_m(_dev(mount="zero_u")) is None


def _plan(racks, aisles, equipment=(), ashrae_class=None) -> FloorPlan:
    return FloorPlan(room_id="room", room_name="Hall",
                     extent=RoomExtent(width_m=6.0, depth_m=6.0),
                     racks=racks, aisles=aisles, equipment=list(equipment),
                     ashrae_class=ashrae_class)


def _frack(rack_id, x, y, facing) -> FloorRack:
    return FloorRack(id=rack_id, name=rack_id, x=x, y=y, facing=facing, device_count=1,
                     offline_count=0, max_severity="CLEAR", w_m=0.6, d_m=1.2, u_height=42)


def test_face_points_land_on_the_face_the_rack_breathes_through():
    # A north-facing rack at y = 3: its intake is at y = 2.4, its exhaust at 3.6.
    plan = _plan([_frack("r", 1.0, 3.0, "N")], [])
    devs = [_dev(id="s1", u_start=5, inlet_c=22.0, exhaust_c=34.0),
            _dev(id="s2", u_start=35, inlet_c=24.0, exhaust_c=36.0)]
    pts = svc.scene_face_points(plan, devs)
    intakes = sorted((p for p in pts if p.side == "intake"), key=lambda p: p.z)
    exhausts = [p for p in pts if p.side == "exhaust"]
    assert [p.y for p in intakes] == [2.4, 2.4]
    assert all(p.y == pytest.approx(3.6) for p in exhausts)
    assert [p.temp_c for p in intakes] == [22.0, 24.0]
    assert intakes[0].z < 0.7 < 1.5 < intakes[1].z     # bottom third, top third


def test_a_rack_without_a_facing_has_no_face_to_draw_on():
    plan = _plan([_frack("r", 1.0, 3.0, None)], [])
    assert svc.scene_face_points(plan, [_dev(u_start=5, inlet_c=22.0)]) == []


def test_scene_indices_grade_against_the_rooms_class():
    plan = _plan([_frack("r", 1.0, 3.0, "N")], [],
                 equipment=[FloorEquipment(id="c", name="CRAH", device_type="crah",
                                           supply_c=18.0, return_c=30.0)],
                 ashrae_class="A2")
    devs = [_dev(u_start=5, inlet_c=30.0, exhaust_c=42.0, power_w=800.0)]
    idx = svc.scene_indices(plan, devs)
    assert idx.ashrae_class == "A2"
    # A2 allows to 35 C: 3 K over 27 against an 8 K span, one intake: 62.5 %.
    assert idx.rci_hi == pytest.approx(62.5)
    assert idx.rti == pytest.approx(100.0)
    assert idx.hot_spots == 1 and idx.racks[0].hot == "allowable"
    assert idx.units_in_ref == 1 and idx.unweighted == 0


def test_an_offline_air_handler_is_not_a_reference():
    plan = _plan([], [], equipment=[
        FloorEquipment(id="c1", name="CRAH 1", device_type="crah", supply_c=18.0, return_c=30.0),
        FloorEquipment(id="c2", name="CRAH 2", device_type="crah", status="OFFLINE",
                       supply_c=25.0, return_c=25.0)])
    assert svc.unit_pairs(plan) == [(18.0, 30.0)]


# --- the field ---------------------------------------------------------------

def test_face_points_collapse_to_one_per_third_of_the_rack():
    pts = tf.face_points(1.0, 2.4, [(0.2, 20.0), (0.4, 22.0), (1.8, 26.0)], "intake", 2.0)
    assert len(pts) == 2
    assert pts[0].z == pytest.approx(0.3) and pts[0].temp_c == 21.0
    assert pts[1].z == pytest.approx(1.8) and pts[1].temp_c == 26.0


def _room_field():
    # Two rows facing each other across a cold aisle (y 1.5-2.5), a hot aisle
    # behind the south row (y 3.7-4.7). The cold faces read 20 C; the hot
    # faces read 40 C. Six metres square.
    pts = []
    for x in (1.0, 2.0, 3.0, 4.0, 5.0):
        pts += tf.face_points(x, 1.5, [(1.0, 20.0)], "intake", 2.0)   # north row, faces south
        pts += tf.face_points(x, 2.5, [(1.0, 20.0)], "intake", 2.0)   # south row, faces north
        pts += tf.face_points(x, 3.7, [(1.0, 40.0)], "exhaust", 2.0)  # south row's rear
    bands = [tf.Band(1.5, 2.5, "cold"), tf.Band(3.7, 4.7, "hot"), tf.Band(0.0, 0.9, "unknown")]
    return tf.field(6.0, 6.0, bands, pts)


def test_the_cold_aisle_is_painted_from_intakes_and_the_hot_from_exhausts():
    f = _room_field()
    nx, c = f["nx"], f["cell_m"]
    mid = f["planes"][1]
    at = lambda x, y: mid["temp"][int(y // c) * nx + int(x // c)]  # noqa: E731
    assert at(3.0, 2.0) == pytest.approx(20.0, abs=0.1)   # cold aisle breathes supply
    assert at(3.0, 4.2) == pytest.approx(40.0, abs=0.1)   # hot aisle carries exhaust
    assert f["intake_points"] == 10 and f["exhaust_points"] == 5


def test_an_exhaust_reading_never_reaches_a_cold_aisle_cell():
    # Every cold-aisle cell is at 20 C: were the 40 C exhausts 1.2 m away
    # allowed in, the cells nearest the south row would read warmer.
    f = _room_field()
    nx, ny, c = f["nx"], f["ny"], f["cell_m"]
    for p in f["planes"]:
        for j in range(ny):
            cy = (j + 0.5) * c
            if 1.5 <= cy <= 2.5:
                row = [p["temp"][j * nx + i] for i in range(nx)]
                ok = all(t is not None and abs(t - 20.0) < 0.05 for t in row)
                assert ok, (p["height_m"], cy, row)


def test_cells_outside_a_classed_aisle_stay_unpainted():
    f = _room_field()
    nx, c = f["nx"], f["cell_m"]
    mid = f["planes"][1]
    assert mid["temp"][int(0.45 // c) * nx + int(3.0 // c)] is None   # the 'unknown' band
    assert mid["temp"][int(3.0 // c) * nx + int(3.0 // c)] is None    # under the south row
    assert mid["conf"][int(3.0 // c) * nx + int(3.0 // c)] == 0.0


def test_confidence_fades_with_distance_from_the_nearest_sensor():
    pts = tf.face_points(1.0, 1.5, [(1.0, 20.0)], "intake", 2.0)
    f = tf.field(6.0, 3.0, [tf.Band(1.5, 2.5, "cold")], pts)
    nx, c = f["nx"], f["cell_m"]
    mid = f["planes"][1]
    j = int(2.0 // c)
    near = mid["conf"][j * nx + int(1.0 // c)]
    far = mid["conf"][j * nx + int(3.0 // c)]
    assert near > far > 0.0
    # Beyond REACH_M along the row nothing informs the cell at all.
    assert mid["temp"][j * nx + int(5.8 // c)] is None


def test_planes_sit_at_the_ashrae_sensor_heights():
    f = tf.field(1.0, 1.0, [], [])
    assert [p["height_m"] for p in f["planes"]] == [0.5, 1.2, 1.8]
    assert f["nx"] == f["ny"] == 4 and f["planes"][0]["temp"] == [None] * 16
