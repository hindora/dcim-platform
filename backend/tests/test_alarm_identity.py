"""One condition, one alarm, however many detectors noticed it.

A campaign across every IT fault type put two `cpu_high` rows on one switch -
MAJOR from the trap, WARNING from the rule - and three rows on one server for a
single hot CPU. Neither was a duplicate as far as the database was concerned:
the alarm key is (device, alarm_type, instance), and `instance` had been
carrying whatever the SOURCE called the reading rather than a part of the
device anybody can point at.

    ""          a server's host MIB
    "ALL"       a switch's Cisco CPU table index
    "CPU Temp"  a BMC's label for the only CPU sensor it has
    (nothing)   a trap, which names no instance at all

Four names for one CPU. The fix is in two halves, because the metrics are not
alike: some genuinely have parts and some do not.
"""

from __future__ import annotations

import inspect

from app.core import alert_taxonomy as tax
from app.core.metrics_gen import DEVICE_SCOPED, METRICS

# ----------------------------------------------------- what has parts, and what does not


def test_a_device_has_one_cpu_load_however_it_is_labelled():
    """`ALL` is a table index, not a component. Neither is an empty string."""
    assert tax.alarm_instance("cpu_utilization", "ALL") == ""
    assert tax.alarm_instance("cpu_utilization", "") == ""
    assert tax.alarm_instance("memory_utilization", "cache") == ""


def test_a_metric_with_real_parts_keeps_them():
    """A dual-socket server reports CPU1 and CPU2, and one of them running hot
    is its own fault with its own fix. Collapsing those would hide the second."""
    assert tax.alarm_instance("cpu_temperature", "CPU1") == "CPU1"
    assert tax.alarm_instance("if_oper_state", "Gi0/1") == "Gi0/1"
    assert tax.alarm_instance("inlet_temperature", "Rack-A17") == "Rack-A17"
    assert tax.alarm_instance("load_pct", "Ckt02") == "Ckt02"


def test_only_the_two_metrics_that_earned_it_are_device_scoped():
    """The default is per-instance on purpose.

    The two mistakes are not equal. A metric wrongly left per-instance shows a
    duplicate, which is visible and irritating. One wrongly collapsed merges two
    genuine faults into a single alarm, and the second fault is then invisible -
    so nothing is device-scoped until somebody says so in the registry.
    """
    assert {"cpu_utilization", "memory_utilization"} == DEVICE_SCOPED
    assert METRICS["if_in_errors"].alarm_scope == "instance"
    assert METRICS["psu_state"].alarm_scope == "instance"
    assert METRICS["alarm_state"].alarm_scope == "instance"


def test_an_unknown_metric_keeps_its_instance():
    """A metric this build has never heard of is not one to start merging."""
    assert tax.alarm_instance("something_new", "Slot3") == "Slot3"
    assert tax.alarm_instance(None, "Slot3") == "Slot3"


# --------------------------------------------------------------- the trap and the rule


def test_a_temperature_trap_files_under_the_rule_that_watches_it():
    """The device's own word for the condition and ours are the same condition.

    SRV04 carried temperature_alert AND cpu_temp_critical AND cpu_temp_high
    through a whole campaign: three rows, one fan.
    """
    assert tax.canonical_alarm_type("temperature_alert") == "cpu_temp_critical"
    # And the ones that were already mapped stay mapped.
    assert tax.canonical_alarm_type("cpu_high_usage") == "cpu_high"
    assert tax.canonical_alarm_type("memory_high_usage") == "memory_high"


def test_the_critical_band_is_the_right_home_for_that_trap():
    """A vendor fires temperature_alert at its own critical point, not at a
    warning - filing it in the lower band would understate what the device
    said about itself."""
    assert tax.canonical_alarm_type("temperature_alert").endswith("_critical")


# ------------------------------------------------- the part-less and the named


def test_a_partless_alarm_folds_under_the_one_that_names_the_part():
    """Where the metric genuinely has parts, the two cannot share a key - so
    the one that says WHICH part is the root, and the vaguer one becomes its
    symptom rather than a second line on the console."""
    src = inspect.getsource(__import__(
        "app.alarms.correlation", fromlist=["x"]).collapse_unqualified)
    assert "instance <> ''" in src or "_QUALIFIED_SIBLING" in src
    assert "mark_symptom" in src


def test_the_fold_works_in_both_arrival_orders():
    """The trap usually arrives first, having been sent the moment the device
    noticed. A poll landing mid-climb beats it often enough to matter."""
    src = inspect.getsource(__import__(
        "app.alarms.correlation", fromlist=["x"]).collapse_unqualified)
    assert "_UNQUALIFIED_SIBLINGS" in src and "_QUALIFIED_SIBLING" in src


def test_the_fold_never_crosses_alarm_types():
    """Two different conditions on one device are two conditions, however close
    together they arrive."""
    from app.alarms import correlation

    for query in (correlation._QUALIFIED_SIBLING,
                  correlation._UNQUALIFIED_SIBLINGS):
        assert "alarm_type = CAST(:alarm_type AS text)" in str(query)


def test_the_root_is_the_worst_of_the_named_alarms():
    """If two sensors are hot, the part-less alarm belongs under the worse one -
    and severity is ranked with the same fragment the alarm list uses, so the
    console cannot disagree with itself about which is worse."""
    from app.alarms import correlation

    sql = str(correlation._QUALIFIED_SIBLING)
    assert "ASC" in sql          # _SEV_RANK numbers CRITICAL 0
    assert "CRITICAL' THEN 0" in sql
