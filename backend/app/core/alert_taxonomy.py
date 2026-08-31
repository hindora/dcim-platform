"""Alert categories: one axis, and what falls out of it.

See docs/18-alert-taxonomy.md. The rule the whole module exists to enforce is
that a category answers ONE question - what kind of thing is wrong, and
therefore who acts - and never "how did we find out" or "which protocol carried
it". A CRAH fan failure is a cooling problem whether it arrived as a BACnet
alarm point or was inferred from a fan speed of zero; a chiller COP anomaly is a
cooling problem whether a threshold or a model noticed it.

Everything the old scheme smuggled into the category axis lives beside it
instead: `severity`, `detection`, `response_class` - alarm or alert, meaning
does this need a response now - the source protocol, and whether the alarm is a
symptom of something upstream.

Classification resolves in three layers, first match wins:

    1. alarm_type          - explicit, for named conditions
    2. (role, metric_key)  - role-sensitive, because the same metric means
                             different things on different equipment
    3. metric group        - the registry's own grouping, as a default

and every condition resolves to one of the seven. There is no residual bucket:
the last resort is VISIBILITY, and it is reachable only by a condition with no
known type, no metric and no device behind it - which is a statement about this
platform's own understanding rather than about any equipment, and therefore
belongs with the other conditions that mean "we cannot see properly".

That the fallback is unreachable in practice is not assumed, it is tested:
`test_alert_taxonomy` fails if any trap, point or alarm type the plane can emit
resolves through it.

This module is the only classifier. The five-bucket scheme it replaced -
thermal / connectivity / datapoint / anomaly / other - ran beside it through
phases 1 to 3 so the API could move ahead of the UI, and was removed with the
phase 4 frontend that stopped reading it.
"""

from __future__ import annotations

from app.core.metrics_gen import DEVICE_SCOPED

# ------------------------------------------------------------------ categories

VISIBILITY = "visibility"
ENVIRONMENTAL = "environmental"
COOLING = "cooling"
POWER = "power"
IT_EQUIPMENT = "it_equipment"
NETWORK = "network"
CAPACITY = "capacity"

CATEGORIES = (VISIBILITY, ENVIRONMENTAL, COOLING, POWER, IT_EQUIPMENT,
              NETWORK, CAPACITY)

#: Where a condition goes when nothing else claims it.
#:
#: Not a bucket of its own: an operator cannot route "uncategorised", and a
#: column that reads zero every day is a column people stop seeing. Reaching
#: this means the condition has no known type, no metric we recognise and no
#: device - so what we actually know is that our own classification failed,
#: which is a visibility problem.
FALLBACK = VISIBILITY

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

#: What each detection method means, in an operator's terms. Served with the
#: legend for the same reason the category text is: a facet an operator cannot
#: define is a facet they will not use.
DETECTION_DESCRIPTIONS: dict[str, dict[str, str]] = {
    THRESHOLD: {
        "label": "Threshold",
        "text": "A measured number crossed a limit we set.",
    },
    STATE: {
        "label": "State",
        "text": "The equipment reported the fault itself - a BACnet or Modbus "
                "alarm point, a trap, a Redfish status.",
    },
    ABSENCE: {
        "label": "Absence",
        "text": "Something stopped arriving. Nothing is reported as wrong; "
                "that is what is wrong.",
    },
    DERIVED: {
        "label": "Derived",
        "text": "Analysis over history concluded it - a ratio, a trend, a "
                "comparison against the rest of the fleet.",
    },
    FORECAST: {
        "label": "Forecast",
        "text": "A projection says it will happen. Nothing has failed yet.",
    },
}

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

# ------------------------------------------------------------ response class

#: Does this condition demand a response NOW, or is it something to schedule?
#:
#: ISA-18.2 and EEMUA 191 draw the line here, and they draw it by required
#: response rather than by how bad the number looks: an ALARM is an abnormal
#: condition that requires operator action and carries an acknowledge
#: lifecycle; an ALERT is informational, needs no action at the console, and
#: belongs to whoever plans maintenance. The distinction exists in the field
#: protocols too - a BACnet notification class carries `notify_type`, which is
#: ALARM or EVENT, set per point at commissioning.
#:
#: This is an ATTRIBUTE, exactly like `detection`, and for the same reason: it
#: answers "how urgently must somebody move", not "what kind of thing is
#: wrong". Making it a category would mean a leak and a dirty filter on the
#: same CDU landed in different buckets, and the plant team would have to read
#: two counters to see their own equipment.
ALARM = "alarm"
ALERT = "alert"

RESPONSE_CLASSES = (ALARM, ALERT)

RESPONSE_DESCRIPTIONS: dict[str, dict[str, str]] = {
    ALARM: {
        "label": "Alarms",
        "text": "Requires a response now. An abnormal condition with a "
                "consequence attached - load at risk, redundancy gone, or the "
                "estate no longer visible. Acknowledging one is a statement "
                "that somebody has taken it.",
    },
    ALERT: {
        "label": "Alerts",
        "text": "Informational. Wear, hygiene and drift - real, worth "
                "scheduling, and not worth waking anybody. It goes to whoever "
                "plans the work rather than to the console.",
    },
}

#: The default, by severity. Severity already encodes consequence in this
#: system - phase 2 sorted the 36 equipment points by it deliberately, with
#: integrity faults that threaten load now as MAJOR and wear as WARNING - so
#: the two axes agree by construction rather than by coincidence.
#:
#: MINOR sits on the alert side: it exists for conditions worth recording that
#: nobody is expected to act on within the shift. A rule that disagrees says so
#: itself; see `alarm_rule.response_class`.
CLASS_BY_SEVERITY: dict[str, str] = {
    "CRITICAL": ALARM,
    "MAJOR": ALARM,
    "MINOR": ALERT,
    "WARNING": ALERT,
    "INFO": ALERT,
}


def response_class_for(severity: str | None, *,
                       rule_class: str | None = None) -> str:
    """Alarm or alert, for one condition.

    An unknown severity resolves to ALARM rather than ALERT, on purpose: the
    failure mode of guessing "alert" is a condition that never reaches the
    console, and silence is the one outcome an alarm system may not produce by
    accident.
    """
    if rule_class in RESPONSE_CLASSES:
        return rule_class
    return CLASS_BY_SEVERITY.get((severity or "").upper(), ALARM)


def response_sql_case(severity_col: str = "a.severity::text",
                      rule_col: str | None = None) -> str:
    """The same defaulting as SQL, for the insert and the backfill.

    `rule_col`, when given, is the rule's override and wins - the CASE is only
    consulted when it is NULL.
    """
    lines = ["CASE"]
    for severity, cls in CLASS_BY_SEVERITY.items():
        lines.append(f"    WHEN {severity_col} = '{severity}' THEN '{cls}'")
    lines.append(f"    ELSE '{ALARM}'")
    lines.append("END")
    case = "\n".join(lines)
    return f"COALESCE({rule_col}, {case})" if rule_col else case


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

    # --- network: the PATH between boxes, not the boxes on it
    #
    # Everything here has blast radius past the device that reported it: a dead
    # link, a dropped adjacency, a port discarding frames. The fix is a cable,
    # an optic, a peer or a config, and the people who own it are looking at a
    # topology, not at a chassis.
    #
    # What is deliberately NOT here: a switch's own CPU, memory, fan or
    # temperature. Those are one box being unwell, they are fixed the same way
    # a server is fixed, and filing them here would make the NETWORK counter
    # answer "is some switch busy" when the question it exists to answer is
    # "is the fabric intact".
    "link_down": NETWORK,
    "link_flap": NETWORK,
    "bgp_session_down": NETWORK,
    "if_errors_high": NETWORK,
    "if_discards_high": NETWORK,

    # --- the box, when the box happens to be network gear
    #
    # These arrive as traps and had no entry, so they fell through to the role
    # layer - which sent every one of them to NETWORK because the device is a
    # switch. A firewall with a pinned control plane was filed as a fabric
    # fault while the same condition on the same box, arriving by poll, was
    # filed as IT equipment. Two categories, one fact.
    "cpu_high_usage": IT_EQUIPMENT,
    "memory_high_usage": IT_EQUIPMENT,
    "device_restarted": IT_EQUIPMENT,
    "server_power_on": IT_EQUIPMENT,   # server_power_off is filed with the rest
    "rack_failure": IT_EQUIPMENT,

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
    # Control-plane saturation on network gear. IT equipment, not network:
    # the category says whose kit is failing, and this is one box misbehaving
    # rather than a path between two of them - the same reason a failed fan in
    # a switch is not a network condition.
    "cpu_saturated": IT_EQUIPMENT,
    "server_cpu_saturated": IT_EQUIPMENT,
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
    # does not have to migrate anything. Listed in RESERVED below, so the
    # legend can say "not built" rather than implying the estate is clean.
    "redundancy_lost": CAPACITY,
    "single_corded_load": CAPACITY,
    "headroom_exhausted": CAPACITY,
    "days_of_supply_low": CAPACITY,
    "pue_excursion": CAPACITY,
}

#: Conditions whose names exist but which nothing raises yet. Reserved so the
#: detector that lands later needs no migration and no UI change - and named
#: here so the legend can mark them, because a category reading zero because it
#: is unimplemented is a different fact from one reading zero because the
#: estate is well.
RESERVED: frozenset[str] = frozenset({
    "redundancy_lost", "single_corded_load", "headroom_exhausted",
    "days_of_supply_low", "pue_excursion",
})

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

#: Device-type role -> category, and the layer that does the real work of
#: making the taxonomy total. A fault on a chiller is a cooling fault even when
#: we know nothing else about it.
BY_ROLE: dict[str, str] = {
    "it": IT_EQUIPMENT,
    # Network gear defaults to the BOX, not the fabric.
    #
    # This was NETWORK, and it made the role layer the largest miscategoriser
    # in the platform: every condition on a switch, router, firewall or load
    # balancer that had no explicit entry - CPU, memory, fan, temperature, a
    # reboot - was filed as a fabric fault. Path conditions still reach NETWORK
    # through their own entries above and through the `interfaces` metric
    # group, both of which are explicit; nothing depends on this default to be
    # called network any more.
    #
    # The asymmetry with cooling and power is deliberate. A chiller has one
    # kind of failure and it is cooling. A switch has two, and they are owned
    # by different people: the fabric team cares that a link is down, the
    # people who own the hardware care that the box is hot.
    "network": IT_EQUIPMENT,
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
    raises, and always returns one of `CATEGORIES`: a condition nothing else
    claims resolves to `FALLBACK`, which is reachable only when the type, the
    metric and the device are all unknown to us.
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

    return FALLBACK


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

    lines.append(f"    ELSE '{FALLBACK}'")
    lines.append("END")
    return "\n".join(lines)


#: How the eight categories group into the five headline counters on the home
#: page. The strip is the wall-display headline and has to stay legible from
#: across a room; the table underneath keeps one column per category, so the
#: grouping hides nothing.
#: One word each. A wall-display headline is read at four metres and in
#: peripheral vision, and "Cooling & Environment" at that distance is a shape,
#: not a word. The two categories inside a group are still counted separately
#: in the table and named in the counter's own tooltip, so nothing is lost -
#: only the ampersand.
STRIP_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("power", "Power", (POWER,)),
    ("cooling_env", "Cooling", (COOLING, ENVIRONMENTAL)),
    ("it_network", "IT", (IT_EQUIPMENT, NETWORK)),
    ("visibility", "Visibility", (VISIBILITY,)),
    ("capacity", "Capacity", (CAPACITY,)),
)


#: Trap event type -> the alarm type this platform already calls it.
#:
#: A condition is one condition however it was noticed. `detection` records HOW
#: - threshold, state, absence - and that is an ATTRIBUTE of the alarm, not a
#: second alarm. Without this map the two detectors raise two rows: a pinned
#: firewall CPU held `cpu_high_usage` from the trap and `cpu_high` from the poll
#: rule at the same time, and an operator had to acknowledge, and later clear,
#: the same fact twice under two names.
#:
#: Only where the two genuinely describe the SAME condition. `fan_failure`
#: arrives by trap and by nothing else; `pdu_temp_high` is a PDU probe rather
#: than a chassis sensor. Aliasing those would merge conditions that are not
#: the same, which is the opposite failure and a worse one.
CANONICAL_ALARM_TYPE: dict[str, str] = {
    # CPU above the alert threshold. The trap fires at the vendor's 90%, the
    # rule at ours - the same condition, seen sooner.
    "cpu_high_usage": "cpu_high",
    # CPU pinned long enough to need answering.
    "cpu_sustained": "cpu_saturated",
    "memory_high_usage": "memory_high",
    # A hot device, reported by the device itself. The rule that watches the
    # same reading calls the condition cpu_temp_critical, and one CPU cooking
    # is one condition however many detectors noticed - SRV04 carried
    # temperature_alert AND cpu_temp_critical AND cpu_temp_high through a whole
    # campaign, three rows for one fan.
    #
    # Mapped to the CRITICAL band because that is what the trap means: the
    # vendor fires it at its own critical point, not at a warning.
    "temperature_alert": "cpu_temp_critical",
    "cpu_temp_critical_trap": "cpu_temp_critical",
}


#: Metrics whose alarms belong to the device rather than to a named part of it.
#:
#: Re-exported from the generated registry so the alarm layer has one import
#: rather than reaching into contracts.
DEVICE_SCOPED_METRICS = DEVICE_SCOPED


def alarm_instance(metric_key: str | None, instance: str) -> str:
    """The instance an alarm on this metric should be filed under.

    `instance` arrives carrying whatever the source called the reading: "" from
    a server's host MIB, "ALL" from a switch's Cisco CPU table, "CPU Temp" from
    a BMC sensor, nothing at all from a trap. For a metric the platform treats
    as device-scoped those are four names for one thing, and since the alarm key
    is (device, alarm_type, instance) they produced four alarms.

    For an instance-scoped metric the label IS the sub-object - Gi0/1, Rack-A17,
    Ckt02 - and collapsing it would merge two genuine faults into one.
    """
    if metric_key and metric_key in DEVICE_SCOPED_METRICS:
        return ""
    return instance


def canonical_alarm_type(alarm_type: str) -> str:
    """The name this platform files a condition under, whoever reported it."""
    return CANONICAL_ALARM_TYPE.get(alarm_type, alarm_type)


def examples_for(category: str, limit: int = 3) -> list[str]:
    """A few alarm types that land in one category.

    Generated from `BY_ALARM_TYPE` rather than written beside it: the legend's
    examples are then the classifier's actual entries, and a condition that
    moves between categories moves in the legend with it.
    """
    found = [t for t, c in BY_ALARM_TYPE.items() if c == category]
    return found[:limit]
