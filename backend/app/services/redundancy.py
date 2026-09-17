"""Where the redundancy is not actually there.

No screen in this product answers this. Every other view asks about one device
at a time, and all three of the findings below are properties of a SET of
paths - invisible from any single device's page, and the reason 2N estates
lose loads that everybody believed were dual fed.

    single_fed   one feed on something that should have two.
    same_side    two feeds, both landing on the same distribution side.
    converged    two sides that meet again at a shared ancestor. Nominally
                 independent, actually one device away from dark.

The third is the one worth building the endpoint for. A load with an A cord
and a B cord looks correct on its own record, on the rack elevation, and in
the trace table, and stays looking correct right up until the RPP they both
trace back to is taken out for breaker work. It is a COMMON MODE, and the only
way to see one is to hold both chains at once.

Note what is NOT here: a load being fed from one side is not automatically
wrong. Plenty of equipment is single-corded by design - a sensor, a fan, a
switch that was never meant to survive a feeder. The endpoint reports what it
finds and names the class; deciding which of them matters is the operator's,
and inventing a policy here would bury that decision in a service.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

#: Below this many converged loads there is no population to generalise from,
#: so nothing is dismissed as the estate's architecture. See
#: `_drop_the_architecture`.
_MIN_TO_GENERALISE = 3


@dataclass
class Graph:
    """One layer, normalised so edges run upstream -> downstream."""

    upstream: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    downstream: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    #: (downstream, upstream) -> side, so a feed can be attributed to a side.
    edge_side: dict[tuple[str, str], str] = field(default_factory=dict)
    nodes: set[str] = field(default_factory=set)

    def add(self, up: str, down: str, side: str | None) -> None:
        self.upstream[down].add(up)
        self.downstream[up].add(down)
        self.nodes.add(up)
        self.nodes.add(down)
        if side:
            self.edge_side[(down, up)] = side

    def sides_of(self, node: str) -> dict[str, set[str]]:
        """side -> the immediate feeders carrying it."""
        out: dict[str, set[str]] = defaultdict(set)
        for up in self.upstream.get(node, ()):
            side = self.edge_side.get((node, up))
            if side:
                out[side].add(up)
        return out


def ancestors(graph: Graph, start: set[str]) -> set[str]:
    """Everything upstream of ``start``, transitively.

    Iterative and visited-guarded: power graphs contain real cycles - a closed
    tie breaker between two boards - and a recursive walk over one does not
    return.
    """
    seen: set[str] = set()
    stack = list(start)
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.upstream.get(node, ()))
    return seen


@dataclass
class Finding:
    device: str
    kind: str
    #: For `converged`, the devices both sides pass through. Empty otherwise.
    shared: list[str] = field(default_factory=list)
    #: The sides actually present on the device's feeds.
    sides: list[str] = field(default_factory=list)
    #: How many other examined devices meet at the same place. A convergence
    #: everybody shares is the estate's architecture; one a handful share is
    #: the defect. Ordering on this is what puts the defect at the top.
    shared_by: int = 0


def _nearest(graph: Graph, shared: set[str]) -> set[str]:
    """The MEET POINT: the shared ancestors closest to the load.

    Everything above a meet point is also shared, so the raw intersection
    reports the whole trunk above it - the switchgear, then the generators,
    then the utility intake. Naming four devices when one of them is the
    answer buries it. A shared node is kept only if nothing else in the shared
    set sits below it.
    """
    below = {n: graph.downstream.get(n, set()) & shared for n in shared}
    return {n for n in shared if not below[n]}


def audit(graph: Graph, candidates: set[str]) -> list[Finding]:
    """Examine every candidate load and report what its feeds are not.

    Candidates are the devices in the requested scope. A device with no feeds
    at all is skipped rather than reported: it is a SOURCE, or it is on no
    circuit anyone recorded, and neither is a redundancy finding - the graph
    endpoint already says how many of those a room has.
    """
    findings: list[Finding] = []

    for dev in sorted(candidates):
        feeds = graph.upstream.get(dev)
        if not feeds:
            continue

        by_side = graph.sides_of(dev)

        # One feed, full stop. Whether that is wrong depends on what it is,
        # which is not this service's call to make.
        if len(feeds) == 1:
            findings.append(Finding(dev, 'single_fed',
                                    sides=sorted(by_side)))
            continue

        # Several feeds, all on one side. Worse than single-fed in one respect:
        # it LOOKS redundant on a cord count, and the second cord buys nothing
        # that the first did not.
        if len(by_side) == 1 and len(feeds) > 1:
            findings.append(Finding(dev, 'same_side', sides=sorted(by_side)))
            continue

        if len(by_side) < 2:
            continue     # unsided feeds - the importer derived no side here

        # Two or more sides. Do they stay apart? Intersecting the ancestor sets
        # is the whole test: anything in every side's ancestry is a device that
        # ALL feeds pass through, which is a single point of failure wearing
        # two cords.
        sets = [ancestors(graph, ups) for ups in by_side.values()]
        shared = set.intersection(*sets)
        shared.discard(dev)
        if shared:
            findings.append(Finding(
                dev, 'converged',
                shared=sorted(_nearest(graph, shared)),
                sides=sorted(by_side)))

    return _drop_the_architecture(findings)


def _drop_the_architecture(findings: list[Finding]) -> list[Finding]:
    """Throw away the convergence that every dual-fed load has.

    EVERY 2N estate converges. The A and B sides come off one switchgear pair,
    which comes off one utility intake and one set of generators, and no amount
    of downstream separation changes that - 2N means independent BELOW the
    point of separation, not above it. Reported literally, the audit fired on
    108 of 137 devices in one hall and named the same switchgear pair on all of
    them. A finding that fires on everything is read once and then ignored,
    which costs more than not having it.

    The discriminator is not a device type and not a depth. It is how MANY
    loads meet at the same place: if more than half the converged loads in
    scope meet at a device, that device is the estate's shape, and if a handful
    do it is a mistake somebody made in one row.

    A MAJORITY, not unanimity. Requiring every converged load to share a meet
    point before calling it structural is defeated by a single load with a
    different one - in the fixture below, five loads meeting at the switchgear
    and a sixth meeting at an RPP left the switchgear at five of six, so
    nothing was dropped and the wolf-crying came straight back.

    The count rides on the findings that survive, so the rarest sort to the
    top: a convergence one load has is a likelier mistake than one twenty
    share.
    """
    converged = [f for f in findings if f.kind == 'converged']
    # You cannot call something "the architecture" from one observation. Below
    # a handful of converged loads there is no population to generalise from,
    # and over-reporting on a tiny scope is the right way to be wrong: a room
    # with two dual-fed loads that both hang off one bad UPS must not have that
    # dismissed as its design.
    if len(converged) < _MIN_TO_GENERALISE:
        return findings

    at: dict[str, int] = defaultdict(int)
    for f in converged:
        for s in f.shared:
            at[s] += 1

    universal = {s for s, n in at.items() if n * 2 > len(converged)}

    kept: list[Finding] = []
    for f in findings:
        if f.kind != 'converged':
            kept.append(f)
            continue
        specific = [s for s in f.shared if s not in universal]
        if not specific:
            # Meets the world only where the world meets. Architecture.
            continue
        f.shared = specific
        f.shared_by = min(at[s] for s in specific)
        kept.append(f)
    return kept
