"""Cable trace over the same hand-built power tree the impact tests use.

    UTIL1 -> SWGR1 -+-> ATS1 -> UPSA -> RPPA -+-> PDUA -+-> srv_dual  (A)
    GEN1  -> SWGR2 -+                         |         +-> srv_single_a
                    +-> ATS2 -> UPSB -> RPPB --> PDUB ----> srv_dual  (B)

Small enough to count the hops by hand, and it carries the two shapes the
walk has to survive: a load reachable by more than one route, and a rank
(the switchgear pair) that feeds both sides.
"""

from __future__ import annotations

from app.services import trace


def edge(eid, up, down, side=None, oper="up", up_term=("none", None),
         down_term=("none", None)):
    return {
        "id": eid, "up": up, "down": down, "redundancy_side": side,
        "oper_state": oper, "link_type": "feeder",
        "up_termination_type": up_term[0], "up_termination_id": up_term[1],
        "down_termination_type": down_term[0], "down_termination_id": down_term[1],
    }


ROWS = [
    edge("e1", "util", "swgr1"),
    edge("e2", "gen1", "swgr2"),
    edge("e3", "swgr1", "ats1", "A"),
    edge("e4", "swgr2", "ats1", "A"),
    edge("e5", "swgr1", "ats2", "B"),
    edge("e6", "swgr2", "ats2", "B"),
    edge("e7", "ats1", "upsa", "A"),
    edge("e8", "ats2", "upsb", "B"),
    edge("e9", "upsa", "rppa", "A"),
    edge("e10", "upsb", "rppb", "B"),
    edge("e11", "rppa", "pdua", "A"),
    edge("e12", "rppb", "pdub", "B"),
    edge("e13", "pdua", "srv_dual", "A",
         up_term=("outlet", "o1"), down_term=("psu", "p1")),
    edge("e14", "pdub", "srv_dual", "B",
         up_term=("outlet", "o2"), down_term=("psu", "p2")),
    edge("e15", "pdua", "srv_single_a", "A"),
]


def build():
    return trace.build(ROWS)


# --- the exit criterion ------------------------------------------------------

def test_a_dual_corded_load_traces_to_both_sides():
    """The answer someone standing at the rack needs: both feeds, named."""
    paths, truncated = trace.trace_up(build(), "srv_dual")
    assert not truncated
    assert {p.side for p in paths} == {"A", "B"}
    assert all(p.verdict == "complete" for p in paths)


def test_every_path_starts_at_a_source_and_ends_at_the_device():
    """Source first, because that is the reading order of a one-line diagram.

    A table that starts at the cord and works outward disagrees with the
    picture beside it, and the reader has to hold two orders at once.
    """
    paths, _ = trace.trace_up(build(), "srv_dual")
    for p in paths:
        assert p.hops[0].edge.up in {"util", "gen1"}
        assert p.hops[-1].edge.down == "srv_dual"


def test_a_dual_corded_load_is_two_chains_not_six():
    """THE regression. Enumerating every root-to-device route multiplies out
    every fork above the device: the switchgear pair with a utility and two
    generators turned two cords into six identical-from-the-ATS-down chains.
    The operator has two cords, so there are two chains."""
    paths, _ = trace.trace_up(build(), "srv_dual")
    assert len(paths) == 2


def test_a_fork_above_the_device_is_named_on_the_hop_it_happens_at():
    """The switchgear pair feeds both ATSs. Losing SWGR1 alone does not drop
    the A side, and that fact has to survive somewhere - it is the entire
    reason the pair is there."""
    paths, _ = trace.trace_up(build(), "srv_dual")
    a = next(p for p in paths if p.side == "A")
    forks = {h.edge.down: set(h.alternates) for h in a.hops if h.alternates}
    # Deterministic: the walk stays on the side it started on and then takes
    # the lowest id, so e3 (swgr1) is the chain and swgr2 is the alternate.
    assert forks == {"ats1": {"swgr2"}}


def test_a_single_corded_load_has_exactly_one_side():
    paths, _ = trace.trace_up(build(), "srv_single_a")
    assert len(paths) == 1
    assert paths[0].side == "A"


def test_a_source_traces_to_nothing_rather_than_failing():
    paths, truncated = trace.trace_up(build(), "util")
    assert paths == []
    assert not truncated


def test_a_device_not_on_this_layer_is_empty_not_an_error():
    paths, truncated = trace.trace_up(build(), "some-cooling-pump")
    assert paths == []
    assert not truncated


# --- the shapes that break a naive walk --------------------------------------

def test_a_cycle_terminates_instead_of_enumerating_forever():
    """Power graphs really do contain loops - a tie breaker closed between two
    boards is one - and a walk that trusts the graph to be a DAG hangs."""
    rows = [
        edge("c1", "a", "b"), edge("c2", "b", "c"), edge("c3", "c", "a"),
        edge("c4", "c", "load"),
    ]
    paths, _ = trace.trace_up(trace.build(rows), "load")
    assert paths, "the walk found no route at all through a cycle"
    for p in paths:
        seen = [h.edge.up for h in p.hops] + [p.hops[-1].edge.down]
        assert len(seen) == len(set(seen)), "a node repeated inside one path"


def test_a_chain_with_no_source_is_incomplete_not_complete():
    """Every node in the ring has a feeder, so nothing in it is a source. The
    verdict has to say the trace never reached one rather than claim it did."""
    rows = [
        edge("c1", "a", "b"), edge("c2", "b", "c"), edge("c3", "c", "a"),
        edge("c4", "c", "load"),
    ]
    paths, _ = trace.trace_up(trace.build(rows), "load")
    assert all(p.verdict == "incomplete" for p in paths)


def test_asymmetry_is_only_reported_when_the_sides_really_differ():
    assert not trace.is_asymmetric(trace.trace_up(build(), "srv_dual")[0])


def test_a_side_extended_by_an_extra_stage_reads_as_asymmetric():
    """The signature of a B side run through one more board during a build and
    never squared up against the A side."""
    rows = [*ROWS, edge("x1", "rppb", "rppb2", "B"),
            edge("x2", "rppb2", "pdub2", "B"),
            edge("x3", "pdub2", "srv_odd", "B"),
            edge("x4", "pdua", "srv_odd", "A")]
    paths, _ = trace.trace_up(trace.build(rows), "srv_odd")
    assert trace.is_asymmetric(paths)


# --- downstream --------------------------------------------------------------

def test_downstream_is_one_hop_not_a_cascade():
    """The cascade belongs to impact analysis, which answers it in the terms
    that matter. Here it is "what is plugged into this"."""
    down = trace.downstream(build(), "pdua")
    assert {e.down for e in down} == {"srv_dual", "srv_single_a"}


def test_terminations_ride_along_on_the_hop():
    """The label is which socket; the trace is useless without it."""
    paths, _ = trace.trace_up(build(), "srv_dual")
    cords = [p.hops[-1].edge for p in paths]
    assert {c.up_termination_id for c in cords} == {"o1", "o2"}
    assert {c.down_termination_id for c in cords} == {"p1", "p2"}
