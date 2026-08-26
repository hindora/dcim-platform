"""NETWORK is the path between boxes. A sick box is IT equipment, wherever it sits.

The categories answer "what kind of thing is wrong, and who owns it". Network
gear is the one class with TWO answers, owned by different people:

* the PATH is broken - a dead link, a dropped adjacency, a port discarding
  frames. Blast radius past the reporting device, fixed with a cable, an optic,
  a peer or a config, and the people who own it are looking at a topology.
* the BOX is unwell - CPU, memory, fan, temperature, a reboot. Fixed the same
  way a server is fixed, by whoever owns the hardware.

`BY_ROLE["network"]` used to send everything with no explicit entry to NETWORK,
which made the role layer the largest miscategoriser here: 46 of the 72 trap
event types have no entry, so a firewall with a pinned control plane was filed
as a fabric fault - while the SAME condition on the SAME box, arriving by poll
instead of by trap, was filed as IT equipment. Two categories, one fact, and a
NETWORK counter answering "is some switch busy" instead of "is the fabric
intact".
"""

from __future__ import annotations

import pytest

from app.core.alert_taxonomy import (
    BY_ALARM_TYPE,
    IT_EQUIPMENT,
    NETWORK,
    classify,
)

# What a device-type's role is called when the classifier is asked.
NET_ROLE = "network"


@pytest.mark.parametrize("alarm_type", [
    "link_down", "link_flap", "bgp_session_down",
    "if_errors_high", "if_discards_high",
])
def test_a_broken_path_is_a_network_condition(alarm_type):
    assert classify(alarm_type, role=NET_ROLE) == NETWORK


@pytest.mark.parametrize("alarm_type", [
    "cpu_high_usage",       # the trap that started this
    "cpu_high",             # and the poll rule for the same fact
    "memory_high_usage",
    "cpu_temp_high",
    "fan_failure",
    "device_restarted",
])
def test_a_sick_box_is_it_equipment_even_on_a_switch(alarm_type):
    assert classify(alarm_type, role=NET_ROLE) == IT_EQUIPMENT


def test_the_two_paths_for_one_fact_agree():
    """A trap and a poll rule describing the same condition must not disagree.

    This is the concrete failure that prompted the change: FW1-DC1-NR-R1-02
    held cpu_high_usage in `network` and cpu_high in `it_equipment` at the same
    time, on one firewall, for one pinned CPU.
    """
    trap = classify("cpu_high_usage", role=NET_ROLE)
    poll = classify("cpu_high", role=NET_ROLE)
    assert trap == poll == IT_EQUIPMENT


def test_an_unknown_condition_on_network_gear_defaults_to_the_box():
    """The role fallback, which is what 46 unclassified trap types rely on.

    Defaulting to NETWORK meant any new vendor trap - a PSU, a licence, a
    watchdog - arrived pre-labelled as a fabric fault.
    """
    assert classify("some_new_vendor_trap", role=NET_ROLE) == IT_EQUIPMENT


def test_interface_metrics_still_reach_network_without_the_role_default():
    """Path conditions must not depend on the role fallback to be called path.

    They are named explicitly, or they arrive with an `interfaces` metric - both
    survive the default changing underneath them.
    """
    assert classify(None, role=NET_ROLE, metric_key="if_in_errors",
                    metric_group="interfaces") == NETWORK


def test_every_path_condition_is_named_rather_than_inferred():
    """If a path type ever loses its entry it silently becomes IT equipment.

    Cheap to assert, and the failure it prevents is a NETWORK counter that
    quietly reads zero during an outage.
    """
    for alarm_type in ("link_down", "link_flap", "bgp_session_down",
                       "if_errors_high", "if_discards_high"):
        assert BY_ALARM_TYPE.get(alarm_type) == NETWORK
