"""Is a finding cooling or power, and what probably caused it."""

from __future__ import annotations

from app.analytics.finding_meta import category_of, cause_of


def test_rack_air_is_environmental_and_the_plant_is_cooling():
    assert category_of("RM-2") == "environmental"
    assert category_of("TH-1") == "environmental"
    assert category_of("CR-1") == "cooling"
    assert category_of("CH-5") == "cooling"


def test_power_includes_a_servers_draw_against_its_workload():
    assert category_of("PD-2") == "power"
    assert category_of("UP-2") == "power"
    assert category_of("SV-4") == "power"
    assert category_of("SV-1") == "it_equipment"
    assert category_of("NW-1") == "network"


def test_a_bad_reading_is_visibility_and_an_unknown_detector_falls_back_to_its_device():
    assert category_of("DQ-4") == "visibility"
    assert category_of("XX-9", "L0") == "visibility"
    assert category_of("XX-9", "L2", "ups") == "power"


def test_no_incident_means_no_cause_upstream():
    assert cause_of({"inc_id": None}) == {"kind": "none"}


def test_a_stopped_crah_is_a_cooling_cause_with_its_peers_and_the_room_caveat():
    c = cause_of({"inc_id": "i1", "inc_state": "resolved", "inc_tier": "unit",
                  "inc_root_device_name": "CRAH1-DC1-HA-R9-06",
                  "inc_evidence": {"root": {"alarm_type": "plant_unit_stopped"},
                                   "peer_alarm_ids": ["a", "b", "c", "d", "e", "f"],
                                   "air_side": "room"}})
    assert c["kind"] == "incident" and c["category"] == "cooling"
    assert c["root_alarm_type"] == "plant_unit_stopped" and c["peers"] == 6
    assert c["by_room"] and c["state"] == "resolved"


def test_a_tripped_feeder_is_a_power_cause_and_a_room_incident_is_airflow():
    assert cause_of({"inc_id": "i", "inc_tier": "power", "inc_evidence": {}})["category"] == "power"
    room = cause_of({"inc_id": "i", "inc_tier": "room", "inc_room_name": "Hall A",
                     "inc_evidence": {}})
    assert room["kind"] == "room" and room["room_name"] == "Hall A"
