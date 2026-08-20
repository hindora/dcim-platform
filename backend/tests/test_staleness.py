"""How long an endpoint may be silent before it counts as silent."""

from __future__ import annotations

import pytest

from app.alarms import staleness as st


def test_grace_is_three_intervals():
    """One missed poll and its retry must not raise an alarm.

    The same reasoning that makes a single failed poll DEGRADED rather than
    OFFLINE: normal jitter is not a fault.
    """
    assert st.grace_seconds(600, False) == 1800


@pytest.mark.parametrize("interval", [10, 30, 60, 90])
def test_fast_polled_endpoints_get_the_floor(interval):
    """BACnet at 10 s would otherwise alarm after 30 s of quiet."""
    assert st.grace_seconds(interval, False) == st.MIN_GRACE_S


def test_a_two_minute_snmp_poll_gets_six_minutes():
    """The fleet's SNMP interval, and the number seen in the live test."""
    assert st.grace_seconds(120, False) == 360


def test_a_very_long_interval_is_capped():
    """Otherwise a daily poll could be silent for three days unnoticed."""
    assert st.grace_seconds(86400, False) == st.MAX_GRACE_S


def test_push_endpoints_get_a_fixed_window():
    """A gNMI subscription streams on change; there is no interval to multiply.

    Zero interval with push enabled is the gnmi-stream profile, and a quiet but
    healthy stream must not read as a dead one.
    """
    assert st.grace_seconds(0, True) == st.PUSH_GRACE_S
    assert st.grace_seconds(None, True) == st.PUSH_GRACE_S


def test_a_zero_interval_without_push_falls_back_to_the_floor():
    """Misconfiguration, not a stream. The floor is the safe reading."""
    assert st.grace_seconds(0, False) == st.MIN_GRACE_S


def test_grace_never_goes_below_the_floor_or_above_the_ceiling():
    for interval in (1, 5, 60, 120, 900, 3600, 86400, 604800):
        g = st.grace_seconds(interval, False)
        assert st.MIN_GRACE_S <= g <= st.MAX_GRACE_S


def test_the_sweep_looks_for_reachable_endpoints_only():
    """The whole point is the pair: polling fine AND delivering nothing.

    A stale endpoint that is also failing to poll is just unreachability, which
    is already alarmed; raising a second alarm for it would be noise.
    """
    sql = str(st._CANDIDATES)
    assert "st.status = 'ONLINE'" in sql
    assert "st.last_success >" in sql


def test_never_reported_needs_the_endpoint_to_have_had_a_chance():
    """Without the age test every endpoint alarms the moment it is imported."""
    assert "e.created_at <" in str(st._CANDIDATES)


def test_messages_distinguish_never_from_stopped():
    """Different faults: one is a mapping gap, the other is something failing."""
    never = st.message({"device_name": "SRV01", "protocol": "snmp",
                        "role": "os_agent", "never_reported": True,
                        "silent_s": None, "grace_s": 360})
    stopped = st.message({"device_name": "SRV01", "protocol": "snmp",
                          "role": "os_agent", "never_reported": False,
                          "silent_s": 1800, "grace_s": 360})
    assert "never" in never
    assert "30 min" in stopped and "never" not in stopped
