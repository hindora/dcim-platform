"""Alarm categories.

The home page groups alarms into a handful of buckets an operator actually
triages by: is the room too hot, has something stopped talking, is the data
itself missing, or is this a plain threshold crossing.

`alarm_type` is a free string chosen by whoever wrote the rule, so the mapping
lives here rather than in the database. One place to edit, and the same
definition is used by the SQL roll-up and by any Python that needs it.

Categories are MUTUALLY EXCLUSIVE - an alarm has exactly one - which is what
lets the per-category counts be summed without double counting. The UI's
"any alarm" indicator is a separate total, not the sum of these.
"""

from __future__ import annotations

# --------------------------------------------------------------- taxonomy

THERMAL = "thermal"
CONNECTIVITY = "connectivity"
DATAPOINT = "datapoint"
ANOMALY = "anomaly"
OTHER = "other"

CATEGORIES = (THERMAL, CONNECTIVITY, DATAPOINT, ANOMALY, OTHER)

#: Exact `alarm_type` -> category. Anything unlisted falls through to the
#: prefix rules below, and then to OTHER.
EXACT: dict[str, str] = {
    # --- thermal: the room, the inlet, the silicon
    "inlet_temp_high": THERMAL,
    "inlet_temp_critical": THERMAL,
    "cpu_temp_high": THERMAL,
    "cpu_temp_critical": THERMAL,
    "ambient_temp_high": THERMAL,
    "humidity_high": THERMAL,
    "humidity_low": THERMAL,

    # --- connectivity: we cannot reach the thing, or the thing that polls it
    "endpoint_unreachable": CONNECTIVITY,
    "collector_stale": CONNECTIVITY,
    "collector_degraded": CONNECTIVITY,
    "assignment_stale": CONNECTIVITY,

    # --- datapoint: reachable, but the value is not arriving.
    #
    # Distinct from connectivity on purpose. A device that answers SNMP while
    # one OID has gone absent is a different fault, with a different fix, from
    # a device that has stopped answering at all - and the pipeline alarms
    # below are the same failure seen from the other end.
    "telemetry_stale": DATAPOINT,
    "ingest_lag_high": DATAPOINT,
    "ingest_stalled": DATAPOINT,
    "ingest_worker_stale": DATAPOINT,
    "db_pool_exhausted": DATAPOINT,
}

#: Fallback prefixes, checked in order. Keeps a newly added rule such as
#: `inlet_temp_warning` in the right bucket without an edit here.
PREFIXES: tuple[tuple[str, str], ...] = (
    ("inlet_temp", THERMAL),
    ("cpu_temp", THERMAL),
    ("ambient_temp", THERMAL),
    ("exhaust_temp", THERMAL),
    ("supply_temp", THERMAL),
    ("return_temp", THERMAL),
    ("humidity", THERMAL),
    ("dew_point", THERMAL),
    ("endpoint_", CONNECTIVITY),
    ("collector_", CONNECTIVITY),
    ("telemetry_", DATAPOINT),
    ("ingest_", DATAPOINT),
    ("anomaly_", ANOMALY),
    ("forecast_", ANOMALY),
)


def categorise(alarm_type: str) -> str:
    """Bucket one `alarm_type`. Never raises; unknown types are OTHER."""
    if alarm_type in EXACT:
        return EXACT[alarm_type]
    for prefix, category in PREFIXES:
        if alarm_type.startswith(prefix):
            return category
    return OTHER


def sql_case(column: str = "a.alarm_type") -> str:
    """The same mapping as a SQL CASE expression.

    Generated rather than hand-written so the roll-up query and `categorise()`
    can never drift apart. Returns a bare expression - the caller supplies the
    alias.
    """
    lines = ["CASE"]
    for alarm_type, category in EXACT.items():
        lines.append(f"    WHEN {column} = '{alarm_type}' THEN '{category}'")
    for prefix, category in PREFIXES:
        lines.append(f"    WHEN {column} LIKE '{prefix}%' THEN '{category}'")
    lines.append(f"    ELSE '{OTHER}'")
    lines.append("END")
    return "\n".join(lines)
