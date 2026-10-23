"""A passive panel is read through the meter clamped on it, named as the meter's."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.devices import _floor_equipment, _metered
from app.services.panel_metering import panel_meter

NOW = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)


def _v(metric: str, instance: str, value, age_s: int = 30) -> dict:
    return {"metric": metric, "instance": instance, "value": value,
            "ts": NOW - timedelta(seconds=age_s)}


LINK = {"meter_id": "m1", "meter_name": "EV21-DC1-HA-R1-04", "status": "ONLINE",
        "last_seen": NOW, "power_w": 35547.0}


def _ev2_values() -> list[dict]:
    return [
        _v("power_draw", "", 35603.0), _v("energy_consumed", "", 137176.0),
        _v("power_factor", "PANEL", 0.975), _v("line_frequency", "", 49.999),
        _v("voltage_thd_pct", "", 3.22), _v("current_thd_pct", "", 5.73),
        _v("current", "A", 61.2), _v("current", "B", 60.9), _v("current", "C", 36.9),
        _v("voltage_ln", "A", 229.8), _v("voltage_ln", "B", 229.9), _v("voltage_ln", "C", 229.7),
        _v("power_draw", "Ckt01", 190.0), _v("current", "Ckt01", 1.16),
        _v("power_factor", "Ckt01", 0.714),
        _v("power_draw", "Ckt04", 8506.0), _v("current", "Ckt04", 37.0),
        _v("power_draw", "Ckt09", 0.0), _v("power_draw", "Ckt10", 0.0),
        _v("alarm_state", "Alarm_Overcurrent", False),
    ]


SCHEDULE = {
    "Ckt01": {"instance": "Ckt01", "branch_id": "p1",
              "branch_name": "PDUA-DC1-HA-R1-01", "label": "PDUA-DC1-HA-R1-01"},
    "Ckt04": {"instance": "Ckt04", "branch_id": "p4",
              "branch_name": "PDUA-DC1-HA-R2-01", "label": "PDUA-DC1-HA-R2-01"},
    "Ckt09": {"instance": "Ckt09", "branch_id": None, "branch_name": None, "label": None},
}


def test_supply_phases_and_circuits_are_split():
    m = panel_meter(LINK, _ev2_values(), SCHEDULE, NOW)
    assert m.power_w == 35603.0
    assert m.energy_kwh == 137176.0
    assert m.power_factor == 0.975
    assert m.current_a == {"A": 61.2, "B": 60.9, "C": 36.9}
    assert m.voltage_ln["B"] == 229.9
    assert [c.instance for c in m.circuits] == ["Ckt01", "Ckt04"]
    assert m.circuits[1].branch_name == "PDUA-DC1-HA-R2-01"
    assert m.circuits[1].power_w == 8506.0
    # Unnamed and carrying nothing: a spare breaker, counted not listed.
    assert m.spare_circuits == 2
    assert m.stale is False


def test_a_loaded_circuit_with_no_schedule_entry_is_still_listed():
    m = panel_meter(LINK, [_v("power_draw", "Ckt12", 950.0)], {}, NOW)
    assert [c.instance for c in m.circuits] == ["Ckt12"]
    assert m.circuits[0].branch_name is None


def test_metering_is_lost_when_the_meter_is_offline_or_old():
    off = panel_meter({**LINK, "status": "OFFLINE"}, _ev2_values(), SCHEDULE, NOW)
    assert off.stale is True
    old = panel_meter(LINK, [_v("power_draw", "", 1.0, age_s=3600)], {}, NOW)
    assert old.stale is True
    assert panel_meter(LINK, [], {}, NOW).stale is True


def test_supply_falls_back_to_the_meters_headline_reading():
    m = panel_meter(LINK, [_v("current", "A", 10.0)], {}, NOW)
    assert m.power_w == 35547.0


def test_borrowed_power_never_lands_in_the_units_own_power():
    unit = {"id": "rpp", "name": "RPPA-DC1-HA-R1-04", "device_type": "rpp", "status": None}
    e = _floor_equipment(unit, [LINK])
    assert e.power_w is None
    assert e.metered_power_w == 35547.0
    assert e.metered_by == "EV21-DC1-HA-R1-04"
    assert e.metered_status == "ONLINE"


def test_an_offline_meter_lends_no_figure():
    f = _metered([{**LINK, "status": "OFFLINE"}])
    assert f["metered_power_w"] is None
    assert f["metered_status"] == "OFFLINE"
    assert _metered(None) == {}
    two = _metered([LINK, {**LINK, "meter_id": "m2", "meter_name": "EV22", "power_w": 100.0}])
    assert two["metered_by"] == "2 meters" and two["metered_power_w"] == 35647.0
