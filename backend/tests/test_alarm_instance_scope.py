"""Instance scope: which samples a rule is allowed to look at.

The bug this covers put twenty permanent MAJOR alarms on the estate. An energy
monitor publishes its feed total AND the branch circuits that add up to it, so
a single condition raised three alarms - and the condition itself was a fixed
45 kW threshold applied to gear that carries 156 kW by design.
"""

from __future__ import annotations

from app.alarms.engine import Rule


def _rule(**over) -> Rule:
    base = {"id": "r1", "name": "power-load-high",
            "alarm_type": "power_load_high", "severity": "MAJOR",
            "message_tpl": "Load {value}%", "metric_key": "load_pct",
            "operator": ">", "threshold": 90.0, "clear_threshold": 80.0,
            "device_total_only": True}
    base.update(over)
    return Rule(**base)


def test_device_total_only_ignores_breakdown_instances():
    rule = _rule()
    assert rule.applies_to_instance("")
    # Ckt01 and Ckt02 are part of the total, not separate failures.
    assert not rule.applies_to_instance("Ckt01")
    assert not rule.applies_to_instance("Ckt02")


def test_per_instance_rules_are_unaffected():
    """Rack inlet sensors are the opposite case: each instance is its own reading."""
    rule = _rule(name="inlet-temp-high", metric_key="inlet_temperature",
                 device_total_only=False)
    assert rule.applies_to_instance("")
    assert rule.applies_to_instance("Rack-A17")


def test_a_rule_scoped_to_types_ignores_the_rest():
    """The retired rule matched every device type, which is how a 45 kW
    threshold reached a switchgear carrying 156 kW."""
    rule = _rule(device_types=("ups", "switchgear"))
    assert rule.applies_to("ups")
    assert not rule.applies_to("server")

    unscoped = _rule(device_types=())
    assert unscoped.applies_to("server")
    assert unscoped.applies_to("switchgear")
