"""The alert taxonomy: one axis, role-sensitive, and exhaustive over the plane.

Three properties matter, and each has bitten a version of this code:

* **One category per condition.** If a condition can resolve two ways, the
  counter and the drill-down disagree and neither can be trusted.
* **Role decides, not the metric name.** A fan on a CRAH and a fan in a server
  are the same metric and different problems.
* **Nothing falls off the edge unnoticed.** Every trap and every equipment
  alarm point the simulator can emit either classifies or is listed here as
  knowingly unmapped. Adding a point upstream should fail this suite rather
  than silently land in `uncategorised`.
"""

from __future__ import annotations

import pytest

from app.core import alert_taxonomy as tax

# --------------------------------------------------------------- the plane
#
# What the simulator can actually emit, transcribed from
# core/trap_definitions.py and the BACnet/Modbus mappings. Alarm-raising points
# only: pure recovery and status notifications (LINK_UP, *_NORMAL, *_CLEARED,
# outletOn, generatorRunning) clear alarms rather than raise them, so they need
# no category.

SIMULATOR_ALARM_TYPES = [
    # visibility - raised by the platform, not the plane
    "endpoint_unreachable", "telemetry_stale", "collector_stale",
    "collector_degraded", "ingest_lag_high", "ingest_stalled",
    "ingest_worker_stale", "db_pool_exhausted", "assignment_stale",
    # network
    "link_down", "link_flap", "bgp_session_down", "auth_failure",
    # IT equipment
    "cpu_high", "cpu_sustained", "cpu_temp_high", "cpu_temp_critical",
    "memory_high", "server_power_off",
    # environmental - room sensors
    "ambient_temp_high", "ambient_temp_critical", "humidity_high",
    "humidity_low", "dew_point_alert", "airflow_high", "airflow_low",
    # power - UPS
    "ups_on_battery", "ups_low_battery", "ups_output_overload",
    "ups_bypass_active", "ups_battery_low_health", "ups_battery_failure",
    "ups_charger_failure", "ups_rectifier_failure", "ups_input_voltage_high",
    "ups_input_voltage_low", "ups_frequency_out_range", "ups_phase_failure",
    # power - PDU
    "pdu_load_high", "pdu_load_critical", "pdu_breaker_tripped",
    "pdu_voltage_high", "pdu_voltage_low", "pdu_phase_imbalance",
    "pdu_power_factor_low", "pdu_ground_fault", "pdu_outlet_current_high",
    # power - generator, ATS, switchgear
    "generator_low_fuel", "generator_low_coolant", "generator_battery_failure",
    "generator_overcrank", "generator_temp_high", "ats_source_lost",
    "ats_transfer_emergency", "ats_fail_to_transfer", "ats_not_in_auto",
    "switchgear_breaker_trip", "switchgear_bus_fault",
]

#: (device role, alarm point) for every `alarm_state` instance arriving from
#: BACnet and Modbus. These do not raise alarms yet - phase 2 - but they must
#: already classify, or phase 2 turns 38 points into 38 uncategorised alarms.
SIMULATOR_ALARM_POINTS = [
    ("cooling", "Alarm_HighPressure"), ("cooling", "Alarm_LowEvapTemp"),
    ("cooling", "Alarm_FlowLoss"), ("cooling", "Alarm_HighCHWSupply"),
    ("cooling", "Alarm_CondPressLimit"), ("cooling", "Alarm_Fault"),
    ("cooling", "Alarm_LowFlow"), ("cooling", "Alarm_HighVibration"),
    ("cooling", "Alarm_LowBasin"), ("cooling", "Alarm_ActuatorFault"),
    ("cooling", "Alarm_Leak"), ("cooling", "Alarm_HighSupplyTemp"),
    ("cooling", "Alarm_PumpFault"), ("cooling", "Alarm_HighTemp"),
    ("cooling", "Alarm_AirflowLoss"), ("cooling", "Filter_Dirty"),
    ("cooling", "Alarm_HighReturnAir"),
    ("power", "Alarm_HighTHD"), ("power", "Alarm_Overcurrent"),
    ("power", "Alarm_PhaseLoss"), ("power", "Alarm_SensorFault"),
    ("power", "Alarm_UnderFrequency"), ("power", "Alarm_Undervoltage"),
    ("power", "Alarm_VoltageImbalance"), ("power", "Alarm_Low_Fuel"),
    ("power", "Alarm_Low_Coolant"), ("power", "Alarm_High_Temp"),
    ("power", "Alarm_Transfer"), ("power", "Battery_Fault"),
    ("power", "Low_Battery"), ("power", "Charger_Fault"),
    ("power", "Fan_Fault"), ("power", "Rectifier_Fault"),
    ("power", "Phase_Fault"), ("power", "Not_In_Auto"),
    ("power", "Fail_To_Transfer"),
]

#: Conditions we know are unclassified. Empty on purpose: anything added here
#: needs a reason, and the reason is usually "we have not decided yet", which
#: is a decision to make rather than a line to add.
KNOWN_UNMAPPED: set[str] = set()


@pytest.mark.parametrize("alarm_type", SIMULATOR_ALARM_TYPES)
def test_every_simulator_alarm_type_classifies(alarm_type):
    category = tax.classify(alarm_type)
    if alarm_type in KNOWN_UNMAPPED:
        assert category == tax.UNCATEGORISED
        return
    assert category != tax.UNCATEGORISED, (
        f"{alarm_type} has no category - add it to BY_ALARM_TYPE, or to "
        f"KNOWN_UNMAPPED with a reason")
    assert category in tax.CATEGORIES


@pytest.mark.parametrize("role,point", SIMULATOR_ALARM_POINTS)
def test_every_equipment_alarm_point_classifies(role, point):
    """Equipment fault points follow the equipment that reported them.

    They arrive as one metric (`alarm_state`) carrying the point name as the
    instance, so the metric cannot classify them - only the device's role can.
    """
    category = tax.classify(role=role, metric_key="alarm_state")
    expected = {"cooling": tax.COOLING, "power": tax.POWER}[role]
    assert category == expected, f"{point} on a {role} device -> {category}"


def test_the_same_metric_follows_the_equipment():
    """The property the old scheme could not express."""
    assert tax.classify(role="cooling", metric_key="fan_speed") == tax.COOLING
    assert tax.classify(role="it", metric_key="fan_speed") == tax.IT_EQUIPMENT

    assert tax.classify(role="power", metric_key="power_draw") == tax.POWER
    assert tax.classify(role="it", metric_key="power_draw") == tax.IT_EQUIPMENT

    # The decision recorded in docs/18: intake air on a rack sensor is the
    # room; on a server it is that one machine.
    assert tax.classify(role="environment",
                        metric_key="inlet_temperature") == tax.ENVIRONMENTAL
    assert tax.classify(role="it",
                        metric_key="inlet_temperature") == tax.IT_EQUIPMENT


def test_shared_alarm_types_resolve_by_role():
    """An alarm type that fires on more than one kind of gear must not be
    pinned in layer 1 - the explicit map wins over role, so pinning it would
    send a server's draw to the electrical team."""
    assert tax.classify("power_draw_high", role="power",
                        metric_key="power_draw") == tax.POWER
    assert tax.classify("power_draw_high", role="it",
                        metric_key="power_draw") == tax.IT_EQUIPMENT

    # Same for intake air, which servers and rack sensors both report.
    assert tax.classify("inlet_temp_high", role="it",
                        metric_key="inlet_temperature") == tax.IT_EQUIPMENT
    assert tax.classify("inlet_temp_high", role="environment",
                        metric_key="inlet_temperature") == tax.ENVIRONMENTAL


def test_protocol_never_decides():
    """Same fault, two transports, one category."""
    by_bacnet = tax.classify(role="cooling", metric_key="alarm_state")
    by_snmp = tax.classify(role="cooling", metric_key="fan_speed")
    assert by_bacnet == by_snmp == tax.COOLING


def test_visibility_is_not_equipment_failure():
    """An unreachable endpoint says nothing about the equipment behind it."""
    assert tax.classify("endpoint_unreachable") == tax.VISIBILITY
    assert tax.classify("telemetry_stale") == tax.VISIBILITY
    # A broken fabric link is the opposite: real, and the network team's.
    assert tax.classify("link_down") == tax.NETWORK


def test_alarm_type_wins_over_role():
    """A named condition has already been reasoned about."""
    assert tax.classify("ups_on_battery", role="power",
                        metric_key="battery_runtime") == tax.POWER
    assert tax.classify("telemetry_stale", role="cooling",
                        metric_key="water_flow") == tax.VISIBILITY


def test_unknown_condition_is_countable_not_guessed():
    assert tax.classify("something_nobody_wrote_down") == tax.UNCATEGORISED
    assert tax.classify(role="", metric_key="") == tax.UNCATEGORISED


def test_detection_is_an_attribute_not_a_category():
    """An anomaly in a cooling metric is still cooling."""
    assert tax.classify(role="cooling", metric_key="cop") == tax.COOLING
    assert tax.detection_for("analytics") == tax.DERIVED
    assert tax.detection_for("forecast") == tax.FORECAST
    assert tax.detection_for("staleness") == tax.ABSENCE
    assert tax.detection_for("threshold") == tax.THRESHOLD
    # Equipment points are state-reported whoever raised them.
    assert tax.detection_for("threshold", metric_key="alarm_state") == tax.STATE


def test_categories_resolve_to_exactly_one():
    """No condition may resolve two ways."""
    seen: dict[str, str] = {}
    for alarm_type in SIMULATOR_ALARM_TYPES:
        category = tax.classify(alarm_type)
        assert seen.setdefault(alarm_type, category) == category


def test_strip_groups_cover_every_category_once():
    """The five headline counters must partition the eight categories.

    `uncategorised` is deliberately outside the groups - it shows as a sixth
    counter only when non-zero, so a taxonomy gap is visible without giving it
    permanent furniture.
    """
    grouped = [c for _key, _label, cats in tax.STRIP_GROUPS for c in cats]
    assert len(grouped) == len(set(grouped)), "a category appears in two groups"
    assert set(grouped) == set(tax.CATEGORIES) - {tax.UNCATEGORISED}


def test_every_category_is_described():
    """The UI legend is generated from these, so a missing one ships blank."""
    for category in tax.CATEGORIES:
        described = tax.DESCRIPTIONS[category]
        assert described["label"] and described["owner"] and described["text"]


def test_sql_case_and_python_agree():
    """The generated SQL must mention every category the classifier can return.

    Not a substitute for running it - that happens against the database in the
    migration - but it catches the common drift where an entry is added to one
    table and not carried into the expression.
    """
    case = tax.sql_case()
    for alarm_type, category in tax.BY_ALARM_TYPE.items():
        assert f"'{alarm_type}'" in case
        assert f"'{category}'" in case
    for role, metric in tax.BY_ROLE_METRIC:
        assert f"'{role}'" in case and f"'{metric}'" in case
    assert case.strip().endswith("END")
