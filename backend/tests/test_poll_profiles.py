"""Poll profiles: shared schedules, and the arithmetic nobody does by hand.

A profile is not one device's setting. `redfish-60s` carries 310 endpoints and
`snmp-bmc-120s` another 310, so a number typed into this form is multiplied by
three hundred before it reaches a network - and the assignment ETag digests
poll settings, so every collector has it within one assignment interval. There
is no staging step between the form and the estate.
"""

from __future__ import annotations

import pytest

from app.core.mappings_gen import MAPPING_GROUPS
from app.services import poll_profile_config as cfg
from app.services.poll_profile_config import PollProfileError

# -------------------------------------------------------------------- names


def test_a_name_is_a_slug_because_the_importer_selects_by_name():
    """app/importer/endpoints.py contains poll_profile="snmp-server-120s".

    A name the importer cannot reproduce is a profile it can never select, and
    the failure shows up at the next import as endpoints landing on some other
    profile rather than as an error at the point of typing.
    """
    assert cfg.validate({"name": "snmp-edge-300s"}, creating=True)["name"] \
        == "snmp-edge-300s"
    for bad in ("SNMP Fast", "snmp fast", "-leading", "trailing-", "ab", ""):
        with pytest.raises(PollProfileError):
            cfg.validate({"name": bad}, creating=True)


def test_a_duplicate_name_is_refused():
    with pytest.raises(PollProfileError):
        cfg.validate({"name": "redfish-60s"}, creating=True,
                     existing_names={"redfish-60s"})


# ---------------------------------------------------------------- intervals


def test_interval_zero_means_the_device_pushes_and_needs_push_enabled():
    """Zero is not "as fast as possible".

    Paired with push it means the DEVICE decides when to send and the scheduler
    must never poll the endpoint - that is the gnmi-stream profile. Without
    push it is an endpoint nothing ever collects, which reports healthy and
    delivers nothing at all.
    """
    assert cfg.validate({"interval_s": 0, "push_enabled": True})["interval_s"] == 0
    with pytest.raises(PollProfileError) as exc:
        cfg.validate({"interval_s": 0, "push_enabled": False})
    assert "push" in str(exc.value)


def test_there_is_a_floor_under_the_interval():
    """One second on a profile with three hundred endpoints behind it is 300
    requests a second at agents that answer one at a time.

    The first symptom is devices going unreachable, which reads as a fleet
    fault rather than as the settings change that caused it.
    """
    with pytest.raises(PollProfileError):
        cfg.validate({"interval_s": 1})
    assert cfg.validate({"interval_s": 10})["interval_s"] == 10


# ------------------------------------------------------- timeout arithmetic


def test_the_worst_case_attempt_has_to_fit_inside_the_interval():
    """(retries + 1) x timeout is how long one endpoint can hold a worker.

    Longer than the interval and the next cycle starts while the last is still
    running: the queue grows, latency climbs, and the endpoint eventually
    reports unreachable for a reason that has nothing to do with the device.
    """
    cfg.check_timing(interval_s=60, timeout_ms=8000, retries=1)      # 16s of 60
    with pytest.raises(PollProfileError) as exc:
        cfg.check_timing(interval_s=10, timeout_ms=6000, retries=2)  # 18s of 10
    assert "longer than" in str(exc.value)


def test_a_pushed_profile_is_exempt_from_the_arithmetic():
    """Nothing is scheduled, so there is no cycle to overrun."""
    cfg.check_timing(interval_s=0, timeout_ms=30_000, retries=3)


# ----------------------------------------------------------- metric groups


def test_a_group_no_mapping_file_defines_is_refused():
    """The failure it prevents is the quietest one in the system.

    The adapter finds no profile block, reads nothing, and the endpoint reports
    healthy with no metrics behind it - indistinguishable from a device that
    has gone quiet, and it stays that way until somebody reads the profile.
    """
    assert cfg.validate({"metric_groups": ["system", "interfaces"]}) \
        ["metric_groups"] == ["system", "interfaces"]
    with pytest.raises(PollProfileError) as exc:
        cfg.validate({"metric_groups": ["sytem"]})
    assert "not a group" in str(exc.value)


def test_the_offered_groups_are_the_ones_the_snmp_mapping_defines():
    """Generated from contracts/mappings/snmp/standard.yaml rather than typed.

    SNMP is the only protocol listed because MetricGroups is read in exactly
    one place in the collector - the SNMP adapter. A group named on a profile
    used by gNMI, BACnet or Modbus selects nothing.
    """
    assert set(MAPPING_GROUPS) == {"snmp"}
    assert {"system", "interfaces", "host_resources"} <= set(MAPPING_GROUPS["snmp"])


def test_duplicates_and_blanks_are_dropped_rather_than_stored():
    assert cfg.validate({"metric_groups": ["system", "system", ""]}) \
        ["metric_groups"] == ["system"]
