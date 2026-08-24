"""Alert categories: one axis, and what falls out of it.

See docs/18-alert-taxonomy.md. The rule the whole module exists to enforce is
that a category answers ONE question - what kind of thing is wrong, and
therefore who acts - and never "how did we find out" or "which protocol carried
it". A CRAH fan failure is a cooling problem whether it arrived as a BACnet
alarm point or was inferred from a fan speed of zero; a chiller COP anomaly is a
cooling problem whether a threshold or a model noticed it.

Everything the old scheme smuggled into the category axis lives beside it
instead: `severity`, `detection`, the source protocol, and whether the alarm is
a symptom of something upstream.

Classification resolves in three layers, first match wins:

    1. alarm_type          - explicit, for named conditions
    2. (role, metric_key)  - role-sensitive, because the same metric means
                             different things on different equipment
    3. metric group        - the registry's own grouping, as a default

and then `uncategorised`, which is deliberate: a point nobody classified has to
be COUNTABLE, not filed into whichever bucket happens to be nearest.

This module does not replace `alarm_categories.py` yet. That one still drives
the home page roll-up and keeps its five buckets until the API and UI move
(phases 3 and 4); running both is what makes phase 1 a no-behaviour-change
step rather than a flag day.
"""

from __future__ import annotations

# ------------------------------------------------------------------ categories

VISIBILITY = "visibility"
ENVIRONMENTAL = "environmental"
COOLING = "cooling"
POWER = "power"
IT_EQUIPMENT = "it_equipment"
NETWORK = "network"
CAPACITY = "capacity"
UNCATEGORISED = "uncategorised"

CATEGORIES = (VISIBILITY, ENVIRONMENTAL, COOLING, POWER, IT_EQUIPMENT,
              NETWORK, CAPACITY, UNCATEGORISED)

#: What each category means and who owns the first five minutes. Shown in the
#: UI legend, so the operator reads the same definition the classifier applies.
DESCRIPTIONS: dict[str, dict[str, str]] = {
    VISIBILITY: {
        "label": "Visibility",
        "owner": "monitoring",
        "text": "We cannot see it. The endpoint is unreachable, telemetry has "
                "stopped arriving, or the collection pipeline itself is "
                "degraded. The equipment may be perfectly healthy.",
    },
    ENVIRONMENTAL: {
        "label": "Environmental",
        "owner": "facilities",
        "text": "The space: room and intake air temperature, humidity, dew "
                "point, leak detection, airflow and containment.",
    },
    COOLING: {
        "label": "Cooling",
        "owner": "plant",
        "text": "Cooling equipment and the loops it serves - CRAH, chiller, "
                "CDU, tower, pump, valve - including staging and plant "
                "capacity.",
    },
    POWER: {
        "label": "Power",
        "owner": "electrical",
        "text": "The electrical chain from utility through ATS, generator, "
                "UPS and switchgear to PDU, RPP and branch circuits.",
    },
    IT_EQUIPMENT: {
        "label": "IT equipment",
        "owner": "IT",
        "text": "The host and its parts: processor, memory, disk, fans, power "
                "supplies, component temperature, predicted hardware failure.",
    },
    NETWORK: {
        "label": "Network",
        "owner": "network",
        "text": "Fabric and transport: link and interface state, error rates, "
                "path redundancy, routing adjacency.",
    },
    CAPACITY: {
        "label": "Capacity",
        "owner": "planning",
        "text": "Headroom and resilience: redundancy lost, single-corded "
                "load, days of supply, efficiency excursions.",
    },
    UNCATEGORISED: {
        "label": "Uncategorised",
        "owner": "triage",
        "text": "Nothing has classified this condition yet. Kept visible on "
                "purpose - an unclassified alarm is a gap in the taxonomy, "
                "and a gap you cannot count is one nobody closes.",
    },
}

# ------------------------------------------------------------------ detection

#: HOW a condition was found. An attribute, never a category - otherwise the
#: same fault changes category when its detection improves, and every new
#: detector adds a bucket nobody can route.
THRESHOLD = "threshold"      # a numeric crossed a limit
STATE = "state"              # equipment reported a fault or state change
ABSENCE = "absence"          # something stopped arriving
DERIVED = "derived"          # analysis over history said so
FORECAST = "forecast"        # projection says it will happen

DETECTIONS = (THRESHOLD, STATE, ABSENCE, DERIVED, FORECAST)

#: Default detection per alarm source, so callers that know nothing more still
#: record something true.
DETECTION_BY_SOURCE: dict[str, str] = {
    "threshold": THRESHOLD,
    "staleness": ABSENCE,
    "comm": ABSENCE,
    "platform": ABSENCE,
    "snmp_trap": STATE,
    "redfish": STATE,
    "bacnet": STATE,
    "modbus": STATE,
    "analytics": DERIVED,
    "forecast": FORECAST,
}

# ------------------------------------------------- layer 1: explicit alarm_type

#: Named conditions. These win over everything: a condition with its own name
#: has already been reasoned about.
BY_ALARM_TYPE: dict[str, str] = {
    # --- visibility: we lost sight of it, which is not the same as it failing
    "endpoint_unreachable": VISIBILITY,
    "telemetry_stale": VISIBILITY,
    "datapoint_missing": VISIBILITY,
    "collector_stale": VISIBILITY,
    "collector_degraded": VISIBILITY,
    "assignment_stale": VISIBILITY,
    "ingest_lag_high": VISIBILITY,
    "ingest_stalled": VISIBILITY,
    "ingest_worker_stale": VISIBILITY,
    "db_pool_exhausted": VISIBILITY,
    "auth_failure": VISIBILITY,

    # --- network: the fabric, not our view of it
    "link_down": NETWORK,
    "link_flap": NETWORK,
    "bgp_session_down": NETWORK,
    "if_errors_high": NETWORK,
    "if_discards_high": NETWORK,

    # --- environmental: the room
    "ambient_temp_high": ENVIRONMENTAL,
    "ambient_temp_critical": ENVIRONMENTAL,
    "humidity_high": ENVIRONMENTAL,
    "humidity_low": ENVIRONMENTAL,
    "dew_point_alert": ENVIRONMENTAL,
    "airflow_high": ENVIRONMENTAL,
    "airflow_low": ENVIRONMENTAL,
    "water_leak": ENVIRONMENTAL,
    "smoke_detected": ENVIRONMENTAL,

    # --- IT equipment: one host
    "cpu_high": IT_EQUIPMENT,
    "cpu_sustained": IT_EQUIPMENT,
    "cpu_temp_high": IT_EQUIPMENT,
    "cpu_temp_critical": IT_EQUIPMENT,
    "memory_high": IT_EQUIPMENT,
    "disk_high": IT_EQUIPMENT,
    "fan_failure": IT_EQUIPMENT,
    "psu_failure": IT_EQUIPMENT,
    "server_power_off": IT_EQUIPMENT,
    "predicted_failure": IT_EQUIPMENT,

    # --- power: the chain, and everything with a countdown attached
    "ups_on_battery": POWER,
    "ups_low_battery": POWER,
    "ups_output_overload": POWER,
    "ups_bypass_active": POWER,
    "ups_battery_low_health": POWER,
    "ups_battery_failure": POWER,
    "ups_charger_failure": POWER,
    "ups_rectifier_failure": POWER,
    "ups_input_voltage_high": POWER,
    "ups_input_voltage_low": POWER,
    "ups_frequency_out_range": POWER,
    "ups_phase_failure": POWER,
    "pdu_load_high": POWER,
    "pdu_load_critical": POWER,
    "pdu_breaker_tripped": POWER,
    "pdu_voltage_high": POWER,
    "pdu_voltage_low": POWER,
    "pdu_phase_imbalance": POWER,
    "pdu_power_factor_low": POWER,
    "pdu_ground_fault": POWER,
    "pdu_outlet_current_high": POWER,
    "power_load_high": POWER,
    # power_draw_high is deliberately NOT here. It fires on servers as well as
    # on distribution gear, and an explicit entry would win over the role - the
    # exact mistake the role layer exists to prevent. Left to resolve by role:
    # power on a PDU, it_equipment on a server.
    "generator_low_fuel": POWER,
    "generator_low_coolant": POWER,
    "generator_battery_failure": POWER,
    "generator_overcrank": POWER,
    "generator_temp_high": POWER,
    "ats_source_lost": POWER,
    "ats_transfer_emergency": POWER,
    "ats_fail_to_transfer": POWER,
    "ats_not_in_auto": POWER,
    "switchgear_breaker_trip": POWER,
    "switchgear_bus_fault": POWER,

    # --- cooling: the plant
    "cooling_unit_fault": COOLING,
    "cooling_leak": COOLING,
    "cooling_low_flow": COOLING,
    "cooling_airflow_loss": COOLING,
    "cooling_filter_dirty": COOLING,
    "chiller_high_pressure": COOLING,
    "chiller_flow_loss": COOLING,
    "cooling_degraded": COOLING,

    # --- capacity: nothing raises these yet; the names are reserved so phase 6
    # does not have to migrate anything
    "redundancy_lost": CAPACITY,
    "single_corded_load": CAPACITY,
    "headroom_exhausted": CAPACITY,
    "days_of_supply_low": CAPACITY,
    "pue_excursion": CAPACITY,
}

# ------------------------------------- layer 2: (device role, metric) -> category

#: The role-sensitive layer, and the reason a metric name alone cannot decide.
#: A fan on a CRAH is cooling; a fan in a server is IT. Power draw on a PDU is
#: the electrical chain; on a server it is the host. Intake air on a rack sensor
#: is the room; on a server it is that machine's own intake.
#:
#: Keys are (device_type.category, metric_key). The device-type categories in
#: use are: it, network, cooling, power, environment, facility.
BY_ROLE_METRIC: dict[tuple[str, str], str] = {}


def _role(role: str, category: str, metrics: tuple[str, ...]) -> None:
    for metric in metrics:
        BY_ROLE_METRIC[(role, metric)] = category


# Shared metric names that mean different things per role.
_AMBIGUOUS = ("fan_speed", "fan_speed_pct", "power_draw", "inlet_temperature",
              "exhaust_temperature", "component_temperature", "load_pct",
              "energy_consumed", "current", "voltage_ln", "voltage_ll",
              "power_factor", "apparent_power", "reactive_power")

_role("it", IT_EQUIPMENT, _AMBIGUOUS)
_role("network", IT_EQUIPMENT, ("fan_speed", "fan_speed_pct",
                                "component_temperature", "psu_state",
                                "psu_input_voltage", "psu_output_power"))
_role("cooling", COOLING, _AMBIGUOUS)
_role("power", POWER, _AMBIGUOUS)
_role("environment", ENVIRONMENTAL, _AMBIGUOUS)

# Facility gateways (BACnet routers, Modbus gateways) carry no process of their
# own; anything they report about themselves is about our ability to see the
# devices behind them.
_role("facility", VISIBILITY, ("reachable", "poll_latency", "sys_uptime"))

# ------------------------------------------- layer 3: metric group -> category

#: `group` from contracts/metrics/registry.yaml. The default when neither the
#: alarm type nor the role/metric pair has an opinion.
BY_METRIC_GROUP: dict[str, str] = {
    "system": VISIBILITY,
    "compute": IT_EQUIPMENT,
    "thermal": IT_EQUIPMENT,      # cpu/component temp, fans - the host
    "interfaces": NETWORK,
    "environment": ENVIRONMENTAL,
    "cooling": COOLING,
    "power": POWER,
}

#: Metric -> group, for the metrics whose group cannot be looked up at call
#: time. Kept small on purpose: the registry is the source of truth, this is
#: only the subset the classifier needs when no group is supplied.
METRIC_GROUP_FALLBACK: dict[str, str] = {
    "reachable": "system", "poll_latency": "system", "sys_uptime": "system",
    "cpu_utilization": "compute", "memory_utilization": "compute",
    "disk_utilization": "compute",
    "cpu_temperature": "thermal", "component_temperature": "thermal",
    "inlet_temperature": "thermal", "exhaust_temperature": "thermal",
    "fan_speed": "thermal", "fan_speed_pct": "thermal",
    "ambient_temperature": "environment", "relative_humidity": "environment",
    "dew_point": "environment", "airflow": "environment",
    "alarm_state": "cooling", "equipment_state": "cooling",
}

#: Device-type role -> category, the last resort before uncategorised. A fault
#: on a chiller is a cooling fault even when we know nothing else about it.
BY_ROLE: dict[str, str] = {
    "it": IT_EQUIPMENT,
    "network": NETWORK,
    "cooling": COOLING,
    "power": POWER,
    "environment": ENVIRONMENTAL,
    "facility": VISIBILITY,
}


def classify(alarm_type: str | None = None, *, role: str | None = None,
             metric_key: str | None = None,
             metric_group: str | None = None) -> str:
    """Resolve one condition to exactly one category.

    `role` is `device_type.category` - what the equipment IS - and it is what
    makes the same metric mean different things on different gear. Never
    raises: an unknown condition is `uncategorised`, which is a finding rather
    than an error.
    """
    if alarm_type and alarm_type in BY_ALARM_TYPE:
        return BY_ALARM_TYPE[alarm_type]

    if role and metric_key and (role, metric_key) in BY_ROLE_METRIC:
        return BY_ROLE_METRIC[(role, metric_key)]

    group = metric_group or METRIC_GROUP_FALLBACK.get(metric_key or "")
    if group and group in BY_METRIC_GROUP:
        # An equipment fault point (`alarm_state`) belongs to the equipment,
        # not to the group its metric was filed under.
        if metric_key in ("alarm_state", "equipment_state") and role in BY_ROLE:
            return BY_ROLE[role]
        return BY_METRIC_GROUP[group]

    if role and role in BY_ROLE:
        return BY_ROLE[role]

    return UNCATEGORISED


def detection_for(source: str | None, *, metric_key: str | None = None) -> str:
    """Default detection method for an alarm, from the source that raised it."""
    if metric_key in ("alarm_state", "equipment_state"):
        return STATE
    return DETECTION_BY_SOURCE.get(source or "", THRESHOLD)


def sql_case(alarm_type_col: str = "a.alarm_type",
             role_col: str = "dt.category",
             metric_col: str = "a.metric_key") -> str:
    """The same three layers as a SQL expression.

    Generated rather than hand-written so the roll-up query and `classify()`
    cannot drift - the drift is invisible until someone compares a counter with
    a drill-down and finds they disagree.

    The caller supplies the joins: `alarm a` to `device` to `device_type dt`.
    """
    lines = ["CASE"]

    for alarm_type, category in BY_ALARM_TYPE.items():
        lines.append(f"    WHEN {alarm_type_col} = '{alarm_type}' THEN '{category}'")

    for (role, metric), category in BY_ROLE_METRIC.items():
        lines.append(f"    WHEN {role_col} = '{role}' AND {metric_col} = '{metric}' "
                     f"THEN '{category}'")

    # Equipment fault points follow the equipment.
    for role, category in BY_ROLE.items():
        lines.append(f"    WHEN {metric_col} IN ('alarm_state', 'equipment_state') "
                     f"AND {role_col} = '{role}' THEN '{category}'")

    for metric, group in METRIC_GROUP_FALLBACK.items():
        category = BY_METRIC_GROUP.get(group)
        if category:
            lines.append(f"    WHEN {metric_col} = '{metric}' THEN '{category}'")

    for role, category in BY_ROLE.items():
        lines.append(f"    WHEN {role_col} = '{role}' THEN '{category}'")

    lines.append(f"    ELSE '{UNCATEGORISED}'")
    lines.append("END")
    return "\n".join(lines)


#: How the eight categories group into the five headline counters on the home
#: page. The strip is the wall-display headline and has to stay legible from
#: across a room; the table underneath keeps one column per category, so the
#: grouping hides nothing. `uncategorised` is deliberately absent - it appears
#: as a sixth counter only when it is non-zero.
STRIP_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("power", "Power", (POWER,)),
    ("cooling_env", "Cooling & Environment", (COOLING, ENVIRONMENTAL)),
    ("it_network", "IT & Network", (IT_EQUIPMENT, NETWORK)),
    ("visibility", "Visibility", (VISIBILITY,)),
    ("capacity", "Capacity", (CAPACITY,)),
)
