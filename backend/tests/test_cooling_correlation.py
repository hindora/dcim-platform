"""The cooling chain: which alarm explains which (docs/33 Phase 2).

Pure: a hand-built site, no database. The live-topology version is
test_cooling_correlation_live.py.
"""

from __future__ import annotations

from app.alarms import cooling_correlation as cc
from app.analytics.correlator import device_summary

T0 = 1_800_000_000.0

# Site DC1: a tower ahead of a chiller, a CHW pump, an MCC feeding the
# chiller, a hall with two CRAHs (one on an MPP), a CDU on a PDU, a rack
# probe, a CRAH's own supply probe, two servers (one on the CDU's loop).
DEVICES = [
    ("tower", "cooling_tower", "plant-room"), ("chiller", "chiller", "plant-room"),
    ("chwp", "pump", "plant-room"), ("mcc", "mcc", "plant-room"),
    ("crah1", "crah", "hall"), ("crah2", "crah", "hall"), ("mpp", "mpp", "hall"),
    ("cdu", "cdu", "hall"), ("pdu", "pdu", "hall"), ("probe", "sensor", "hall"),
    ("supply", "sensor", "hall"), ("srv_air", "server", "hall"),
    ("srv_liq", "server", "hall"), ("netsrv", "server", "net-room"),
]
COOLING = [("tower", "chiller"), ("chiller", "chwp"), ("chwp", "tower"),
           ("crah1", "supply"), ("supply", "crah1"),
           ("cdu", "srv_liq"), ("srv_liq", "cdu")]
POWER = [("mcc", "chiller"), ("mpp", "crah1"), ("pdu", "cdu"), ("pdu", "srv_air")]


def graph() -> cc.CoolingGraph:
    return cc.CoolingGraph.build(
        [{"id": i, "name": i.upper(), "device_type": t, "room_id": r, "datacenter_id": "dc1"}
         for i, t, r in DEVICES], COOLING, POWER)


def alarm(id_, dev, typ, sev="MAJOR", t=T0, category=None, instance=""):
    return {"id": id_, "device_id": dev, "alarm_type": typ, "severity": sev,
            "first_seen": t, "category": category, "instance": instance}


# --- the graph ---------------------------------------------------------------------

def test_a_server_is_cooled_by_its_loop_its_room_and_the_plant_above():
    anc = graph().ancestors("srv_liq")
    assert anc["cdu"] == "unit" and anc["crah1"] == "unit" and anc["crah2"] == "unit"
    assert anc["chiller"] == "plant" and anc["chwp"] == "plant"
    assert anc["mpp"] == "power" and anc["pdu"] == "power"     # feeders of its units
    assert "srv_air" not in anc


def test_a_cdu_cools_its_loop_not_the_room_air():
    anc = graph().ancestors("probe")
    assert "crah1" in anc and "cdu" not in anc
    assert "cdu" not in graph().ancestors("srv_air")


def test_a_units_own_probe_answers_to_that_unit_only():
    anc = graph().ancestors("supply")
    assert "crah1" in anc and "crah2" not in anc


def test_a_room_with_no_units_is_cooled_by_the_site_plant():
    anc = graph().ancestors("netsrv")
    assert set(anc) >= {"chiller", "tower", "chwp"} and "crah1" not in anc


def test_inside_the_plant_only_what_feeds_a_machine_is_upstream():
    anc = graph().ancestors("chiller")
    assert anc == {"tower": "plant", "mcc": "power"}


# --- choosing the root -------------------------------------------------------------

def test_a_stopped_crah_explains_the_hot_rack_probe():
    g = graph()
    sym = alarm("s", "probe", "ambient_temp_high", "MAJOR", T0 + 120)
    root = cc.pick_root(g, sym, [alarm("r", "crah1", "plant_unit_stopped")])
    assert root["id"] == "r" and root["tier"] == "unit" and root["suppress"]


def test_a_consequence_worse_than_its_cause_stays_visible():
    g = graph()
    sym = alarm("s", "srv_air", "cpu_temp_critical", "CRITICAL", T0 + 300)
    root = cc.pick_root(g, sym, [alarm("r", "crah1", "crah_high_temp", "MAJOR")])
    assert root["id"] == "r" and not root["suppress"]


def test_only_the_first_worse_consequence_of_a_kind_stays_visible():
    g = graph()
    root = alarm("r", "crah1", "crah_high_temp", "MAJOR")
    first = alarm("s1", "srv_air", "inlet_temp_critical", "CRITICAL", T0 + 60)
    again = alarm("s2", "srv_liq", "inlet_temp_critical", "CRITICAL", T0 + 90)
    # Keyed by root, ROOM and kind: srv_air and srv_liq share the hall.
    reps = {("r", "hall", "inlet_temp_critical"): "s1"}
    assert cc.pick_root(g, first, [root], representatives=reps)["suppress"] is False
    got = cc.pick_root(g, again, [root], representatives=reps)
    assert got["suppress"] and not got["representative"]
    # A different kind of consequence gets its own visible line.
    cpu = alarm("s3", "srv_air", "cpu_temp_critical", "CRITICAL", T0 + 120)
    assert cc.pick_root(g, cpu, [root], representatives=reps)["representative"]


def test_a_server_inlet_alarm_is_heat_by_name_or_by_metric():
    g = graph()
    root = [alarm("r", "crah1", "plant_unit_stopped")]
    assert cc.pick_root(g, alarm("s", "srv_air", "inlet_temp_high", "WARNING", T0 + 60), root)
    custom = {**alarm("s", "srv_air", "hall_b_custom_rule", "WARNING", T0 + 60),
              "metric_key": "inlet_temperature"}
    assert cc.pick_root(g, custom, root)
    assert cc.might_take_part("hall_b_custom_rule", None, "inlet_temperature")


def test_the_highest_tier_wins_then_the_earliest():
    g = graph()
    sym = alarm("s", "probe", "ambient_temp_high", t=T0 + 600)
    roots = [alarm("crah", "crah1", "crah_high_temp", t=T0 + 60, category="cooling"),
             alarm("chl", "chiller", "plant_unit_stopped", t=T0 + 120)]
    assert cc.pick_root(g, sym, roots)["id"] == "chl"
    two = [alarm("b", "crah2", "crah_high_temp", t=T0 + 90, category="cooling"),
           alarm("a", "crah1", "crah_high_temp", t=T0 + 60, category="cooling")]
    assert cc.pick_root(g, sym, two)["id"] == "a"


def test_a_crah_discharging_warm_answers_to_the_tripped_chiller():
    g = graph()
    sym = alarm("s", "crah1", "crah_high_temp", t=T0 + 200, category="cooling")
    root = cc.pick_root(g, sym, [alarm("r", "chiller", "plant_unit_stopped")])
    assert root["tier"] == "plant"


def test_one_crah_is_not_the_cause_of_another():
    g = graph()
    sym = alarm("s", "crah2", "crah_high_temp", t=T0 + 200, category="cooling")
    assert cc.pick_root(g, sym, [alarm("r", "crah1", "plant_unit_stopped")]) is None


def test_the_tower_ahead_of_a_chiller_is_its_cause():
    g = graph()
    sym = alarm("s", "chiller", "chiller_high_pressure", t=T0 + 200, category="cooling")
    root = cc.pick_root(g, sym, [alarm("r", "tower", "plant_unit_stopped")])
    assert root["id"] == "r"


def test_a_tripped_mcc_explains_the_chiller_but_a_named_breaker_does_not():
    g = graph()
    sym = alarm("s", "chiller", "plant_unit_stopped", t=T0 + 30)
    assert cc.pick_root(g, sym, [alarm("r", "mcc", "breaker_tripped")])["tier"] == "power"
    named = alarm("r", "mcc", "breaker_tripped", instance="CB-3")
    assert cc.pick_root(g, sym, [named]) is None


def test_a_dirty_filter_explains_nothing():
    g = graph()
    sym = alarm("s", "probe", "ambient_temp_high", t=T0 + 60)
    assert cc.pick_root(g, sym, [alarm("r", "crah1", "crah_filter_dirty",
                                       category="cooling")]) is None


def test_a_fire_stopped_crah_is_not_called_a_cooling_fault():
    g = graph()
    sym = alarm("s", "probe", "ambient_temp_high", t=T0 + 60)
    roots = [alarm("r", "crah1", "plant_unit_stopped")]
    assert cc.pick_root(g, sym, roots, fire_rooms=frozenset({"hall"})) is None
    assert cc.pick_root(g, sym, roots) is not None


def test_the_root_may_arrive_late_but_not_too_late_or_too_early():
    g = graph()
    sym = alarm("s", "probe", "ambient_temp_high", t=T0)
    late = cc.LEAD_S - 60
    assert cc.pick_root(g, sym, [alarm("r", "crah1", "plant_unit_stopped", t=T0 + late)])
    assert cc.pick_root(g, sym, [alarm("r", "crah1", "plant_unit_stopped",
                                       t=T0 + cc.LEAD_S + 60)]) is None
    assert cc.pick_root(g, sym, [alarm("r", "crah1", "plant_unit_stopped",
                                       t=T0 - cc.LAG_S - 60)]) is None


def test_a_non_thermal_alarm_downstream_is_left_alone():
    g = graph()
    sym = alarm("s", "srv_air", "psu_failure", t=T0 + 60)
    assert cc.pick_root(g, sym, [alarm("r", "crah1", "plant_unit_stopped")]) is None


def test_the_raise_path_skips_by_name_before_touching_anything():
    assert cc.might_take_part("ambient_temp_high", None)
    assert cc.might_take_part("crah_high_temp", None)            # trap: no category yet
    assert cc.might_take_part("equipment_alarm", None)
    assert not cc.might_take_part("psu_failure", None)
    assert not cc.might_take_part("crah_filter_dirty", "cooling")
    assert not cc.might_take_part("endpoint_unreachable", "visibility")


# --- what the incident says --------------------------------------------------------

def test_the_summary_says_what_was_folded_and_what_was_kept():
    members = [{"tier_of_member": "zone", "is_symptom": True},
               {"tier_of_member": "zone", "is_symptom": True},
               {"tier_of_member": "server", "is_symptom": False}]
    title, summary = device_summary("CRAH1", "plant_unit_stopped", members, 1, True)
    assert title == "CRAH1: plant unit stopped"
    assert "3 downstream alarms (2 rack and room sensors, 1 IT equipment)" in summary
    assert "2 folded under it" in summary
    assert "1 more severe than the cause kept visible, one of each kind" in summary
    assert "1 anomaly finding on the same chain" in summary
    assert "tied to the unit by room" in summary


def test_a_cause_cleared_inside_the_hold_still_explains_new_heat():
    """all-chillers 2026-11-04: each chiller's alarm cleared as the sequencer
    moved on, while the chilled water stood at 19 C."""
    g = graph()
    sym = alarm("s", "probe", "ambient_temp_high", t=T0 + 300)
    gone = {**alarm("c", "chiller", "plant_unit_stopped", t=T0), "cleared": True}
    assert cc.pick_root(g, sym, [gone])["id"] == "c"
    live = alarm("o", "chiller", "plant_unit_stopped", t=T0 + 120)
    # An open cause of the same tier wins over a cleared one, though later.
    assert cc.pick_root(g, sym, [gone, live])["id"] == "o"


def test_each_room_gets_its_own_voice():
    """all-chillers 2026-11-04: one critical line for the whole site told the
    people in the other hall nothing. One per room and kind."""
    g = graph()
    root = alarm("r", "chiller", "plant_unit_stopped", "MAJOR")
    reps = {("r", "hall", "inlet_temp_critical"): "s1"}
    other_room = alarm("s9", "netsrv", "inlet_temp_critical", "CRITICAL", T0 + 60)
    got = cc.pick_root(g, other_room, [root], representatives=reps)
    assert got["representative"] and not got["suppress"]


def test_a_cleared_cause_speaks_for_nothing():
    g = graph()
    gone = {**alarm("c", "chiller", "plant_unit_stopped", "MAJOR"), "cleared": True}
    warm = alarm("s", "probe", "ambient_temp_high", "WARNING", T0 + 60)
    got = cc.pick_root(g, warm, [gone])
    assert got["id"] == "c" and got["representative"]       # the first one is seen
