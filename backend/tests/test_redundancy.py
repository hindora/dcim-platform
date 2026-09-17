"""The redundancy audit, over the DC1 power tree the impact tests use.

    UTIL1 -> SWGR1 -+-> ATS1 -> UPSA -> RPPA -+-> PDUA -> srv_dual   (A)
    GEN1  -> SWGR2 -+                         +-> PDUA -> srv_single
                    +-> ATS2 -> UPSB -> RPPB --> PDUB -> srv_dual    (B)

srv_dual is genuinely dual fed down to the switchgear pair, which both sides
share - so it is ALSO a convergence, and that is correct: losing both boards
takes it out. The findings are facts about the graph, not a grade.
"""

from __future__ import annotations

from app.services import redundancy


def build(extra=()) -> redundancy.Graph:
    g = redundancy.Graph()
    edges = [
        ("util", "swgr1", None), ("gen1", "swgr2", None),
        ("swgr1", "ats1", "A"), ("swgr2", "ats1", "A"),
        ("swgr1", "ats2", "B"), ("swgr2", "ats2", "B"),
        ("ats1", "upsa", "A"), ("ats2", "upsb", "B"),
        ("upsa", "rppa", "A"), ("upsb", "rppb", "B"),
        ("rppa", "pdua", "A"), ("rppb", "pdub", "B"),
        ("pdua", "srv_dual", "A"), ("pdub", "srv_dual", "B"),
        ("pdua", "srv_single", "A"),
        *extra,
    ]
    for up, down, side in edges:
        g.add(up, down, side)
    return g


def find(findings, device):
    return next((f for f in findings if f.device == device), None)


# --- the three findings ------------------------------------------------------

def test_a_single_corded_load_is_reported():
    f = find(redundancy.audit(build(), {"srv_single"}), "srv_single")
    assert f and f.kind == "single_fed"


def test_two_cords_on_the_same_side_is_not_redundancy():
    """Worse than single-fed in one respect: it LOOKS right on a cord count,
    and the second cord buys nothing the first did not."""
    g = build(extra=[("pdua2", "srv_bad", "A"), ("pdua", "srv_bad", "A"),
                     ("rppa", "pdua2", "A")])
    f = find(redundancy.audit(g, {"srv_bad"}), "srv_bad")
    assert f and f.kind == "same_side"
    assert f.sides == ["A"]


def test_two_sides_that_meet_again_are_a_convergence():
    """Both RPPs hung off one UPS. The load has an A cord and a B cord and is
    one device away from dark, which no per-device page can show."""
    g = redundancy.Graph()
    for up, down, side in [
        ("upsa", "rppa", "A"), ("upsa", "rppb", "B"),
        ("rppa", "pdua", "A"), ("rppb", "pdub", "B"),
        ("pdua", "srv", "A"), ("pdub", "srv", "B"),
    ]:
        g.add(up, down, side)
    f = find(redundancy.audit(g, {"srv"}), "srv")
    assert f and f.kind == "converged"
    assert "upsa" in f.shared


def test_the_shared_ancestor_is_named_not_just_counted():
    """The finding is only actionable if it says WHICH device to look at."""
    g = redundancy.Graph()
    for up, down, side in [
        ("ups", "rppa", "A"), ("ups", "rppb", "B"),
        ("rppa", "srv", "A"), ("rppb", "srv", "B"),
    ]:
        g.add(up, down, side)
    f = find(redundancy.audit(g, {"srv"}), "srv")
    assert f.shared == ["ups"]


# --- what must NOT be reported ----------------------------------------------

def test_a_source_is_not_a_finding():
    """Nothing feeds a utility intake. That is not a redundancy defect."""
    assert redundancy.audit(build(), {"util"}) == []


def test_a_device_on_no_circuit_is_not_a_finding():
    """A sensor with no cord is the graph endpoint's business - it already
    counts them - and reporting it here would bury the real findings."""
    assert redundancy.audit(build(), {"not-in-the-graph"}) == []


def test_unsided_feeds_are_left_alone():
    """Where the importer derived no side there is nothing to say about
    sides, and guessing one would invent a finding."""
    g = redundancy.Graph()
    g.add("a", "load", None)
    g.add("b", "load", None)
    assert redundancy.audit(g, {"load"}) == []


def test_a_truly_independent_pair_reports_nothing():
    """Two sides that never touch. The audit has to be quiet here or nobody
    will read it when it is not."""
    g = redundancy.Graph()
    for up, down, side in [
        ("utila", "upsa", "A"), ("utilb", "upsb", "B"),
        ("upsa", "srv", "A"), ("upsb", "srv", "B"),
    ]:
        g.add(up, down, side)
    assert redundancy.audit(g, {"srv"}) == []


# --- the shapes that break a naive walk --------------------------------------

def test_a_cycle_in_the_ancestry_terminates():
    """A closed tie breaker between two boards is a real cycle, and an
    ancestor walk that trusts the graph to be a DAG does not return."""
    g = redundancy.Graph()
    for up, down, side in [
        ("a", "b", "A"), ("b", "c", "A"), ("c", "a", "A"),
        ("c", "srv", "A"), ("z", "srv", "B"),
    ]:
        g.add(up, down, side)
    findings = redundancy.audit(g, {"srv"})
    assert isinstance(findings, list)


def test_only_the_meet_point_is_named_not_the_whole_trunk_above_it():
    """Everything above a meet point is also shared, so the raw intersection
    reports the switchgear AND the generators AND the utility. Naming four
    devices when one of them is the answer buries it."""
    f = find(redundancy.audit(build(), {"srv_dual"}), "srv_dual")
    assert f and f.kind == "converged"
    assert set(f.shared) == {"swgr1", "swgr2"}, (
        "the trunk above the meet point was reported as well")
    assert f.sides == ["A", "B"]


# --- not crying wolf ---------------------------------------------------------

def test_the_convergence_every_load_shares_is_the_architecture_not_a_finding():
    """EVERY 2N estate converges - the A and B sides come off one switchgear
    pair off one utility intake, and no downstream separation changes that.
    Reported literally this fired on 108 of 137 devices in one hall and named
    the same pair every time. A report that fires on everything is read once."""
    loads = [(f"srv{i}", "A", "B") for i in range(6)]
    extra = []
    for name, a, b in loads:
        extra += [("pdua", name, a), ("pdub", name, b)]
    findings = redundancy.audit(build(extra), {n for n, _, _ in loads})
    assert [f for f in findings if f.kind == "converged"] == []


def test_a_convergence_only_one_load_has_survives_the_cull():
    """The whole point. Five loads converge where everybody converges; the
    sixth ALSO meets at an RPP of its own, and that one is the mistake."""
    loads = [(f"srv{i}") for i in range(6)]
    extra = []
    for name in loads[:5]:
        extra += [("pdua", name, "A"), ("pdub", name, "B")]
    # srv5's two "sides" both hang off RPPA.
    extra += [("rppa", "pduX", "B"), ("pduX", "srv5", "B"),
              ("pdua", "srv5", "A")]
    findings = redundancy.audit(build(extra), set(loads))
    bad = [f for f in findings if f.kind == "converged"]
    assert [f.device for f in bad] == ["srv5"]
    assert "rppa" in bad[0].shared
    assert bad[0].shared_by == 1


def test_a_tiny_scope_is_not_generalised_from():
    """Two dual-fed loads that both hang off one bad UPS must not have that
    dismissed as the design. One observation is not a population."""
    g = redundancy.Graph()
    for up, down, side in [
        ("ups", "rppa", "A"), ("ups", "rppb", "B"),
        ("rppa", "srv1", "A"), ("rppb", "srv1", "B"),
        ("rppa", "srv2", "A"), ("rppb", "srv2", "B"),
    ]:
        g.add(up, down, side)
    findings = redundancy.audit(g, {"srv1", "srv2"})
    assert {f.device for f in findings} == {"srv1", "srv2"}
    assert all(f.shared == ["ups"] for f in findings)
