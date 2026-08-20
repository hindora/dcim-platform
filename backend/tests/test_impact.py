"""Impact analysis over a hand-built power tree.

The tree is the real DC1 shape, small enough to reason about by hand:

    UTIL1 -> SWGR1 -+-> ATS1 -> UPSA -> RPPA -+-> PDUA -> srv_dual
    GEN1  -> SWGR2 -+                         +-> PDUA -> srv_single_a
                    +-> ATS2 -> UPSB -> RPPB --> PDUB -> srv_dual
"""

from __future__ import annotations

from app.services import impact


def build() -> impact.Graph:
    g = impact.Graph()
    edges = [
        ("util", "swgr1", None), ("gen1", "swgr2", None),
        ("swgr1", "ats1", "A"), ("swgr1", "ats2", "B"),
        ("swgr2", "ats1", "A"), ("swgr2", "ats2", "B"),
        ("ats1", "upsa", "A"), ("ats2", "upsb", "B"),
        ("upsa", "rppa", "A"), ("upsb", "rppb", "B"),
        ("rppa", "pdua", "A"), ("rppb", "pdub", "B"),
        ("pdua", "srv_dual", "A"), ("pdub", "srv_dual", "B"),
        ("pdua", "srv_single_a", "A"),
    ]
    for up, down, side in edges:
        g.add(up, down, side)
    return g


def names(nodes):
    return sorted(nodes)


# --- the exit criterion ------------------------------------------------------

def test_losing_a_side_cuts_off_only_the_single_corded_load():
    """The list an operator actually needs before a maintenance window.

    Taking UPSA out drops every A-side load to one feed, which is the accepted
    cost of the window. The single-corded load on that side goes dark, and that
    is the short list someone has to act on first.
    """
    r = impact.analyse(build(), "upsa", "power")
    assert "srv_single_a" in r.cut_off
    assert "srv_dual" not in r.cut_off, (
        "a dual-fed server was reported as losing power while its B feed was intact")


def test_the_dual_fed_load_is_reported_as_degraded_not_cut_off():
    r = impact.analyse(build(), "upsa", "power")
    assert "srv_dual" in r.degraded


def test_cut_off_cascades_through_the_distribution_below_it():
    """Killing UPSA also kills RPPA and PDUA, not just the leaf loads.

    Answering one hop deep would report the PDU as fine because it "has a
    feeder" - a feeder that is itself only fed through the candidate.
    """
    r = impact.analyse(build(), "upsa", "power")
    assert {"rppa", "pdua", "srv_single_a"} <= r.cut_off


# --- redundancy accounting ---------------------------------------------------

def test_a_single_fed_load_is_never_called_degraded():
    """It had nothing to lose; it is either fed or it is not."""
    r = impact.analyse(build(), "upsa", "power")
    assert "srv_single_a" not in r.degraded


def test_removing_one_rack_pdu_degrades_but_does_not_cut():
    r = impact.analyse(build(), "pdub", "power")
    assert r.cut_off == set()
    assert "srv_dual" in r.degraded


# --- surviving paths ---------------------------------------------------------

def test_losing_utility_cuts_almost_nothing_because_the_generator_backs_it():
    """Both switchgear lineups feed both transfer switches.

    This is the answer for a sustained state: a path from a source still
    reaches the load. It says nothing about the transfer delay, which is a
    property of the ATS rather than of the graph.
    """
    r = impact.analyse(build(), "util", "power")
    assert r.cut_off == {"swgr1"}, names(r.cut_off)
    assert "srv_dual" not in r.cut_off and "srv_single_a" not in r.cut_off


def test_a_single_source_chain_loses_everything_below_it():
    """With only one way in, removing it takes the whole chain."""
    g = impact.Graph()
    for up, down in (("util", "swgr"), ("swgr", "ats"), ("ats", "ups"),
                     ("ups", "rpp"), ("rpp", "pdu"), ("pdu", "srv")):
        g.add(up, down, "A")
    r = impact.analyse(g, "util", "power")
    assert r.cut_off == {"swgr", "ats", "ups", "rpp", "pdu", "srv"}


def test_a_device_with_no_modelled_feed_counts_as_a_source():
    """A real limitation, pinned deliberately rather than discovered later.

    "Source" means "nothing in the graph feeds it" - which is how the utility
    feeds and generators are identified without hard-coding device types. The
    cost is that a device whose feed was never imported looks like an infinite
    supply, and anything below it is then reported as surviving a cut that
    would really take it out. Impact analysis is therefore only as complete as
    the connection import.
    """
    g = impact.Graph()
    g.add("util", "swgr1", None)
    g.add("swgr1", "load", "A")
    g.add("orphan", "load", "B")      # orphan has no feed of its own
    assert "orphan" in g.sources()
    r = impact.analyse(g, "util", "power")
    assert "load" not in r.cut_off, "the orphan still feeds it, per the graph"


# --- shape -------------------------------------------------------------------

def test_a_leaf_device_has_no_dependents():
    r = impact.analyse(build(), "srv_dual", "power")
    assert r.dependents == set()
    assert r.cut_off == set() and r.degraded == set()


def test_dependents_counts_everything_downstream_not_just_the_casualties():
    r = impact.analyse(build(), "upsa", "power")
    assert r.dependents >= r.cut_off | r.degraded
    assert "srv_dual" in r.dependents


def test_every_layer_has_a_stated_effect():
    """The verdict is only as strong as the layer's semantics.

    Losing power is not the same class of event as losing monitoring, and the
    response says which one it means rather than leaving the caller to guess.
    """
    for layer in impact.LAYERS:
        assert layer in impact.LAYER_EFFECT
        assert impact.LAYER_EFFECT[layer]


def test_degraded_is_power_only():
    """No other layer here has a labelled second path.

    Management and fieldbus devices have one route; claiming a redundancy
    verdict for them would be inventing one.
    """
    g = impact.Graph()
    g.add("sw", "host_a", None)
    g.add("sw", "host_b", None)
    r = impact.analyse(g, "sw", "management")
    assert r.degraded == set()
    assert r.cut_off == {"host_a", "host_b"}
