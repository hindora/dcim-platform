"""Which constraint binds, and what counts as a load.

Both are easy to get subtly wrong in ways that produce a confident number.
"""

from __future__ import annotations

from app.services import capacity as c


def con(name: str, used: float | None, cap: float | None, unit: str = "kW") -> c.Constraint:
    return c.Constraint(name=name, unit=unit, used_p95=used, capacity=cap,
                        capacity_source=c.MEASURED if cap else c.UNKNOWN)


# --- binding -----------------------------------------------------------------

def test_the_most_utilised_known_constraint_binds():
    worst, why = c.binding([con("power", 50, 100), con("space", 90, 100)])
    assert worst.name == "space"
    assert "90%" in why


def test_a_constraint_with_no_limit_cannot_bind():
    """Unknown is not "fine". A rack with no power rating is unmeasured, and
    treating it as unlimited is how a room gets filled past its breakers."""
    worst, why = c.binding([con("power", 500, None), con("space", 10, 100)])
    assert worst.name == "space"
    assert "power" in why and "no rating recorded" in why


def test_nothing_known_binds_nothing_and_says_so():
    worst, why = c.binding([con("power", 500, None), con("cooling", 100, None)])
    assert worst is None
    assert "no constraint has a known limit" in why


def test_the_answer_names_what_it_could_not_judge():
    """Silence about an unmeasured constraint reads as an all-clear."""
    _, why = c.binding([con("power", 5, None), con("space", 10, 100)])
    assert "something else may bind first" in why


def test_a_zero_capacity_is_treated_as_unknown_not_as_full():
    assert con("space", 5, 0).known is False


# --- utilisation -------------------------------------------------------------

def test_headroom_and_utilisation_need_both_numbers():
    known = con("space", 25, 100)
    assert known.headroom == 75
    assert known.utilisation_pct == 25.0
    unknown = con("power", 25, None)
    assert unknown.headroom is None and unknown.utilisation_pct is None


def test_tight_flags_before_anything_has_actually_bound():
    assert con("space", 85, 100).tight is True
    assert con("space", 50, 100).tight is False


# --- what counts as a load ---------------------------------------------------

def test_distribution_gear_is_not_a_load():
    """A UPS reports the power flowing THROUGH it to the racks. Adding that to
    the racks' own draw counts the same kilowatts twice - it made a datacenter
    read 994 kW against a real end load of 148."""
    for conduit in ("ups", "pdu", "rpp", "switchgear", "ats", "mcc", "mpp",
                    "utility_feed", "generator"):
        assert conduit not in c.LOAD_TYPES


def test_meters_are_not_loads_either():
    """Less obvious, and it cost 479 kW: a branch-circuit monitor's power_draw
    is what it MEASURES on its circuits, not what the meter consumes."""
    assert "energy_monitor" not in c.LOAD_TYPES


def test_cooling_load_is_it_heat_not_total_facility():
    """The chillers and their pumps are the cooling system; counting their draw
    as heat they must reject inflates the load by a third."""
    for plant in ("chiller", "crah", "pump", "cooling_tower"):
        assert plant not in c.IT_TYPES
    assert "server" in c.IT_TYPES


def test_the_percentile_is_the_planning_one():
    """Sizing on the momentary peak strands capacity; sizing on the mean
    under-provisions for the hours that matter."""
    assert c.PERCENTILE == 95
