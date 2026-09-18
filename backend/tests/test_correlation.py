"""Correlation rules that need no database.

The live behaviour is covered by test_correlation_live.py against the real
topology; these pin the decisions that would silently produce a wrong answer
rather than an error.
"""

from __future__ import annotations

from app.alarms import correlation as c

# --- the redundancy veto -----------------------------------------------------

def test_healthy_second_feed_means_no_suppression():
    """The exit criterion for this phase, in one assertion.

    A load whose A feed failed but whose B feed is fine is still running, so
    nothing about it is explained by the feed failure.
    """
    assert c.has_surviving_feed({"A": True, "B": False}) is True


def test_every_feed_gone_means_the_load_is_explained():
    assert c.has_surviving_feed({"A": True, "B": True}) is False


def test_single_corded_load_is_explained_by_its_only_feed():
    assert c.has_surviving_feed({"A": True}) is False
    assert c.has_surviving_feed({"A": False}) is True


def test_a_load_with_no_known_feeds_is_never_explained():
    """No evidence is not evidence of a cause.

    An empty map means nothing feeds this device in the graph, which is a gap
    in the data rather than a reason to hide an alarm.
    """
    assert c.has_surviving_feed({}) is False


def test_an_undetermined_path_does_not_count_as_redundancy():
    """'?' is a feed whose side could not be derived.

    It is grouped separately rather than merged into A or B: if it is healthy
    the load may well still be fed, but it must never make two compromised
    sides look survivable.
    """
    assert c.has_surviving_feed({"A": True, "?": True}) is False
    assert c.has_surviving_feed({"A": True, "?": False}) is True


# --- what may be suppressed --------------------------------------------------

def test_only_cannot_see_it_alarms_are_suppressible():
    assert "endpoint_unreachable" in c.SUPPRESSIBLE_TYPES


def test_staleness_is_not_suppressible_even_though_it_looks_like_it_should_be():
    """It is raised only for endpoints that ARE reachable.

    telemetry_stale means "the poll succeeds and returns nothing", so an
    upstream visibility failure cannot be its cause. Suppressing it anyway did
    the wrong thing in testing: an endpoint that had polled successfully 287
    seconds earlier was folded under an unreachable OOB switch, hiding the one
    condition the staleness sweep exists to surface.
    """
    assert "telemetry_stale" not in c.SUPPRESSIBLE_TYPES


def test_real_device_conditions_are_never_suppressible():
    """A hot CPU behind a dead switch is still a hot CPU.

    Folding a condition about the device itself under a comms root would lose
    a genuine fault the moment its management path breaks.
    """
    for alarm_type in ("cpu_temp_critical", "inlet_temp_high", "power_draw_high",
                       "humidity_low", "memory_high"):
        assert alarm_type not in c.SUPPRESSIBLE_TYPES


# --- graph direction ---------------------------------------------------------

def test_management_points_the_opposite_way_to_power_and_fieldbus():
    """The single most breakable assumption in this module.

    A managed device holds its own cable end (a=device, b=switch), while a
    gateway or a PDU owns the trunk (a=gateway/feeder, b=device/load). Reading
    every layer the same way yields an engine that quietly explains nothing,
    because the traversal walks away from the cause instead of towards it.
    """
    assert c._UPSTREAM_COL["management"] == ("b_device_id", "a_device_id")
    assert c._UPSTREAM_COL["fieldbus"] == ("a_device_id", "b_device_id")
    assert c._UPSTREAM_COL["power"] == ("a_device_id", "b_device_id")


def test_every_searched_layer_has_a_direction():
    for layer in c.LAYER_ORDER:
        assert layer in c._UPSTREAM_COL


def test_power_is_searched_last():
    """Power is the only layer whose root can be vetoed by redundancy.

    Cheaper, unconditional explanations are tried first.
    """
    assert c.LAYER_ORDER[-1] == "power"


def test_direction_columns_are_never_caller_supplied():
    """They are interpolated into SQL, so they must come from this fixed map."""
    for up, down in c._UPSTREAM_COL.values():
        assert {up, down} == {"a_device_id", "b_device_id"}


# --- what counts as a root, per layer ----------------------------------------

def test_a_breaker_trip_is_a_power_root():
    """It opens the circuit: everything corded to it is off, however healthy
    the PDU's own controller still looks."""
    assert "breaker_tripped" in c.root_types("power")
    assert "switchgear_breaker_trip" in c.root_types("power")


def test_an_unreachable_feeder_is_still_a_power_root():
    assert "endpoint_unreachable" in c.root_types("power")


def test_a_breaker_trip_explains_nothing_on_the_management_plane():
    """A tripped strip says nothing about which switch a device is watched
    through; only visibility failures are management roots."""
    assert c.root_types("management") == ("endpoint_unreachable",)
    assert c.root_types("fieldbus") == ("endpoint_unreachable",)


def test_a_degraded_feed_is_not_a_dead_one():
    """On battery, overloaded or out of voltage, the load is still fed. Taking
    any of those as a root would fold live faults under a feed that is still
    delivering."""
    for alarm_type in ("ups_on_battery", "ups_output_overload",
                       "voltage_low", "load_high"):
        assert alarm_type not in c.root_types("power")


def test_link_down_is_explained_only_through_its_far_end():
    """Not by the upstream walk - nothing upstream of a spine explains its
    port - so it is not in the generic suppressible set."""
    assert "link_down" in c.LINK_TYPES
    assert "link_down" not in c.SUPPRESSIBLE_TYPES


async def test_a_link_down_that_names_no_port_is_never_explained():
    """No port, no cable, no far end: returns before touching the database."""
    assert await c.correlate(None, alarm_id="x", device_id="y",
                             alarm_type="link_down", instance="") is None


# --- restarts ----------------------------------------------------------------

def test_a_restart_is_a_point_event_with_a_short_life():
    from app.alarms import reconcile
    assert "device_restarted" in reconcile.EVENT_TYPES
    assert reconcile.EVENT_GRACE_S < reconcile.REASSERT_GRACE_S


def test_the_restore_window_covers_a_slow_boot_and_not_the_next_day():
    assert 300 <= c.RESTORE_WINDOW_S <= 1800
