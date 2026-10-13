"""The BMS-trunk instruments (docs/27 §8b.3, §8c.2) classify where their
owners sit, and the security category exists for the door."""

from __future__ import annotations

from app.core import alert_taxonomy as tx
from app.core.metrics_gen import METRICS


def test_security_is_a_category_with_words_of_its_own():
    assert tx.SECURITY in tx.CATEGORIES
    assert tx.DESCRIPTIONS[tx.SECURITY]["owner"] == "security"
    assert set(tx.DESCRIPTIONS) == set(tx.CATEGORIES)


def test_each_instrument_condition_is_filed_with_the_thing_that_fails():
    cases = {
        "door_held_open": tx.SECURITY,
        "hydrogen_high": tx.ENVIRONMENTAL, "hydrogen_critical": tx.ENVIRONMENTAL,
        "battery_temp_high": tx.POWER, "battery_temp_critical": tx.POWER,
        "bulk_fuel_low": tx.POWER, "bulk_fuel_critical": tx.POWER,
        "makeup_water_low": tx.COOLING, "makeup_water_critical": tx.COOLING,
        "transformer_oil_temp_high": tx.POWER, "transformer_oil_temp_critical": tx.POWER,
        "transformer_winding_temp_high": tx.POWER, "transformer_winding_temp_critical": tx.POWER,
    }
    for alarm_type, category in cases.items():
        # Whatever the sensor's role says, the type decides.
        assert tx.classify(alarm_type, role="environment", metric_key="x") == category, alarm_type


def test_the_thresholds_are_threshold_crossings_and_the_door_is_not():
    for t in ("hydrogen_high", "battery_temp_critical", "bulk_fuel_low", "makeup_water_critical",
              "transformer_oil_temp_high", "transformer_winding_temp_critical"):
        assert t in tx.THRESHOLD_CROSSINGS
    assert "door_held_open" not in tx.THRESHOLD_CROSSINGS


def test_the_registry_carries_the_instrument_keys_for_sensors():
    for key in ("door_open", "hydrogen_lel_pct", "water_level_pct", "water_volume", "fuel_volume"):
        assert "sensor" in METRICS[key].device_types, key
    assert "sensor" in METRICS["battery_temperature"].device_types
    assert "sensor" in METRICS["fuel_level_pct"].device_types
    assert METRICS["door_open"].value_type == "bool" and METRICS["door_open"].group == "security"


def test_the_registry_knows_the_transformer_monitor():
    for key in ("oil_temperature", "winding_temperature", "tap_position", "load_pct", "current"):
        assert "transformer" in METRICS[key].device_types, key
    assert METRICS["oil_temperature"].group == "power"
    assert tx.BY_METRIC_GROUP["security"] == tx.SECURITY
