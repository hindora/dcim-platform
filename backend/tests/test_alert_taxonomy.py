"""The alert taxonomy: one axis, role-sensitive, and exhaustive over the plane.

Three properties matter, and each has bitten a version of this code:

* **One category per condition.** If a condition can resolve two ways, the
  counter and the drill-down disagree and neither can be trusted.
* **Role decides, not the metric name.** A fan on a CRAH and a fan in a server
  are the same metric and different problems.
* **Nothing falls off the edge unnoticed.** Every trap and every equipment
  alarm point the simulator can emit either classifies or is listed here as
  knowingly unmapped. Adding a point upstream should fail this suite rather
  than silently land in whichever category the fallback happens to be.
"""

from __future__ import annotations

import pathlib
import re

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
    # power - PDU. These are the names that arrive ON THE WIRE, which is not
    # what the simulator calls them internally: only the two load conditions
    # carry a pdu_ prefix through the trap contract, and the rest are the bare
    # vendor names shared with UPS and RPP gear. Transcribing the simulator
    # vocabulary here instead put seven names in BY_ALARM_TYPE that nothing
    # could ever match, and the mistake was invisible because classify() cannot
    # fail - it just resolved them by role.
    "pdu_load_high", "pdu_load_critical", "breaker_tripped",
    "voltage_high", "voltage_low", "phase_imbalance",
    "power_factor_low", "ground_fault", "outlet_current_high",
    "outlet_failure", "outlet_off", "pdu_temp_high", "pdu_humidity_high",
    # power - generator, ATS, switchgear
    "generator_low_fuel", "generator_low_coolant", "generator_battery_failure",
    "generator_overcrank", "generator_temp_high", "ats_source_lost",
    "ats_transfer_emergency", "ats_fail_to_transfer", "ats_not_in_auto",
    "switchgear_breaker_trip", "switchgear_bus_fault",
]

#: (device role, alarm point) for every `alarm_state` instance arriving from
#: BACnet and Modbus. These do not raise alarms yet - phase 2 - but they must
#: already classify, or phase 2 turns 38 points into 38 misfiled alarms.
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

#: Conditions deliberately left to the role layer, which is NOT the same as
#: unclassified. Each of these arrives from more than one kind of power gear -
#: voltage_high is an Eaton UPS notification and a rack PDU one, breaker_tripped
#: comes from PDUs and RPPs - and BY_ALARM_TYPE is keyed by name alone, so an
#: explicit entry would win over the role for every device that sends it. That
#: is the mistake the role layer exists to prevent; power_draw_high is left out
#: of the table for the same reason.
ROLE_RESOLVED: set[str] = {
    "breaker_tripped", "voltage_high", "voltage_low", "phase_imbalance",
    "power_factor_low", "ground_fault", "outlet_current_high",
    "outlet_failure", "outlet_off",
}


@pytest.mark.parametrize("alarm_type", SIMULATOR_ALARM_TYPES)
def test_every_simulator_alarm_type_classifies(alarm_type):
    """Explicitly, not by falling through to the fallback.

    Since the residual bucket was removed, EVERY condition resolves to a real
    category - including one nobody classified, which lands on `FALLBACK`. So
    "it has a category" no longer proves anything; what has to hold is that the
    classifier names this condition on purpose.
    """
    if alarm_type in KNOWN_UNMAPPED:
        assert alarm_type not in tax.BY_ALARM_TYPE
        return
    if alarm_type in ROLE_RESOLVED:
        # Named nowhere on purpose, and still landing somewhere real: the
        # device that sent it decides, which is the whole point of the layer.
        assert alarm_type not in tax.BY_ALARM_TYPE, (
            f"{alarm_type} is shared across power gear, so an explicit entry "
            f"would override the role for every device that sends it")
        for role in ("power", "ups"):
            assert tax.classify(alarm_type, role=role) in tax.CATEGORIES
        return
    assert alarm_type in tax.BY_ALARM_TYPE, (
        f"{alarm_type} is not named in BY_ALARM_TYPE, so it would land in "
        f"{tax.FALLBACK} by accident - add it, or add it to KNOWN_UNMAPPED "
        f"with a reason")
    assert tax.classify(alarm_type) in tax.CATEGORIES


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


def test_an_unknown_condition_lands_in_visibility():
    """There is no residual bucket, so the fallback has to be defensible.

    Reaching it means we have no type, no metric we recognise and no device -
    what we know is that our own classification failed, which is a statement
    about our view of the estate rather than about any equipment. It is also
    unreachable for anything the plane can actually emit; that is what
    `test_every_simulator_alarm_type_classifies` is for.
    """
    assert tax.classify("something_nobody_wrote_down") == tax.VISIBILITY
    assert tax.classify(role="", metric_key="") == tax.VISIBILITY
    assert tax.FALLBACK in tax.CATEGORIES


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
    """The five headline counters must partition the seven categories.

    Partition, not cover: a category in two groups would be counted twice on
    the strip and once in the table, and a category in none would be countable
    in the table and invisible on the strip.
    """
    grouped = [c for _key, _label, cats in tax.STRIP_GROUPS for c in cats]
    assert len(grouped) == len(set(grouped)), "a category appears in two groups"
    assert set(grouped) == set(tax.CATEGORIES)


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


# ------------------------------------------------- the table has no dead names
#
# Conditions this platform can file that do NOT arrive as an SNMP trap: the
# threshold rules in the rules table, the points that reach us over BACnet and
# Modbus, and the ones the platform raises about itself. Kept here rather than
# read from the database so the check stays a unit test - the rules table is
# seeded by migration and is not available to import.
NON_TRAP_ALARM_TYPES = frozenset({
    # raised by the platform about its own health or its sight of the plane
    "endpoint_unreachable", "telemetry_stale", "datapoint_missing",
    "collector_degraded", "collector_stale", "assignment_stale",
    "ingest_lag_high", "ingest_stalled", "ingest_worker_stale",
    "db_pool_exhausted",
    # threshold rules over polled telemetry
    "cpu_high", "cpu_saturated", "server_cpu_saturated", "cpu_temp_high",
    "memory_high", "disk_high", "if_errors_high", "if_discards_high",
    "ambient_temp_high", "ambient_temp_critical", "humidity_high",
    "humidity_low", "airflow_high", "airflow_low", "power_load_high",
    # equipment points arriving over BACnet / Modbus rather than SNMP
    "cooling_unit_fault", "cooling_leak", "cooling_low_flow",
    "cooling_airflow_loss", "cooling_filter_dirty", "cooling_degraded",
    "water_leak", "psu_failure", "server_power_on",
    "ups_low_battery", "ups_battery_failure", "ups_battery_low_health",
    "ups_bypass_active", "ups_charger_failure", "ups_rectifier_failure",
    "ups_input_voltage_high", "ups_input_voltage_low",
    "ups_frequency_out_range",
    # analysis and capacity
    "predicted_failure", "pue_excursion", "redundancy_lost",
    "single_corded_load", "days_of_supply_low", "headroom_exhausted",
})


def _trap_contract_event_types() -> set[str]:
    """Every event name the trap contract can put on an alarm."""
    path = (pathlib.Path(__file__).resolve().parents[2]
            / "contracts" / "mappings" / "snmp" / "traps.yaml")
    raw = path.read_text(encoding="utf-8")
    names = set(re.findall(r"^\s+event_type:\s*(\S+)", raw, re.M))
    for group in re.findall(r"clears:\s*\[([^\]]*)\]", raw):
        names |= {n.strip() for n in group.split(",") if n.strip()}
    # Both forms: BY_ALARM_TYPE legitimately names either side of a
    # canonicalisation (cpu_high_usage is what the wire says, cpu_high is what
    # we file it as), and both are things something can raise.
    return names | {tax.canonical_alarm_type(n) for n in names}


def test_no_unreachable_alarm_types():
    """Every explicit entry names a condition something can actually raise.

    An entry that matches nothing is worse than no entry: it reads as a
    deliberate classification while the condition is in fact resolving by role,
    so the table documents an intention the code never carries out. Seven
    pdu_-prefixed names sat here doing exactly that, transcribed from the
    simulator catalogue rather than from the vocabulary the collector emits.
    """
    reachable = _trap_contract_event_types() | NON_TRAP_ALARM_TYPES
    dead = sorted(set(tax.BY_ALARM_TYPE) - reachable)
    assert not dead, (
        f"BY_ALARM_TYPE names conditions nothing can raise: {dead}. Either wire "
        f"the condition up, or delete the entry and let the role resolve it.")


@pytest.mark.parametrize("event_type", sorted(_trap_contract_event_types()))
def test_every_trap_event_type_classifies(event_type):
    """A trap the collector can map always lands in a real category."""
    assert tax.classify(event_type, role="power") in tax.CATEGORIES


def test_a_vendor_threshold_trap_is_a_threshold_detection():
    """Detection describes the condition, not the pipe it arrived down.

    A PDU firing loadHigh compared a current against its own breaker rating.
    Filed as `state` because it travelled as a trap, it vanished from a
    detection=threshold filter that its polled twin answered.
    """
    assert tax.detection_for("snmp_trap", metric_key="current",
                             alarm_type="pdu_load_high") == tax.THRESHOLD
    assert tax.detection_for("threshold", metric_key="load_pct",
                             alarm_type="power_load_high") == tax.THRESHOLD


def test_a_state_trap_stays_a_state_detection():
    """A reading carried as CONTEXT does not make a state change a crossing.

    An open breaker reports ~0 A and a failed outlet reports nothing at all;
    neither was found by comparing a number with a limit.
    """
    for alarm_type in ("breaker_tripped", "outlet_failure", "smoke_detected",
                       "ground_fault"):
        assert tax.detection_for("snmp_trap", metric_key="current",
                                 alarm_type=alarm_type) == tax.STATE


def test_threshold_crossings_are_classifiable():
    """Nothing in the crossing set is a name the taxonomy cannot place."""
    for alarm_type in tax.THRESHOLD_CROSSINGS:
        assert tax.classify(alarm_type, role="power") in tax.CATEGORIES
