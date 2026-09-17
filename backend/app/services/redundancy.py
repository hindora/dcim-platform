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


def nearest_shared(graph: Graph, shared: set[str]) -> set[str]:
    """The MEET POINT: the shared ancestors closest to the load.

    Everything above a meet point is also shared, so the raw intersection
    reports the whole trunk above it - the switchgear, then the generators,
    then the utility intake. Naming four devices when one of them is the
    answer buries it. A shared node is kept only if nothing else in the shared
    set sits below it.
    """
    below = {n: graph.downstream.get(n, set()) & shared for n in shared}
    return {n for n in shared if not below[n]}


def _meet_points(graph: Graph, dev: str) -> set[str]:
    """Where this device's distribution sides come back together, if they do."""
    by_side = graph.sides_of(dev)
    if len(by_side) < 2:
        return set()
    shared = set.intersection(*(ancestors(graph, ups) for ups in by_side.values()))
    shared.discard(dev)
    return nearest_shared(graph, shared) if shared else set()


def structural(graph: Graph) -> tuple[set[str], dict[str, int]]:
    """The meet points that are the ESTATE'S SHAPE rather than a mistake.

    Computed over the whole layer, not over the requested scope. The
    architecture is a property of the estate, and scoping the question to one
    room makes it unanswerable there: a plant room with two dual-fed switches
    has no population to generalise from, so the switchgear pair every load in
    the building shares came back as a finding on both of them. Asking the
    layer instead gets the same answer from every room.

    A MAJORITY, not unanimity. Requiring every converged load to share a meet
    point is defeated by a single load with a different one: five loads at the
    switchgear and a sixth at an RPP leaves the switchgear at five of six,
    nothing is structural, and the wolf-crying comes straight back.
    """
    at: dict[str, int] = defaultdict(int)
    total = 0
    for dev in graph.nodes:
        points = _meet_points(graph, dev)
        if not points:
            continue
        total += 1
        for p in points:
            at[p] += 1
    if total < _MIN_TO_GENERALISE:
        # Nothing to generalise from. Over-reporting on a graph this small is
        # the right way to be wrong: two loads that both hang off one bad UPS
        # must not have that dismissed as the design.
        return set(), at
    return {p for p, n in at.items() if n * 2 > total}, at


def audit(graph: Graph, candidates: set[str]) -> list[Finding]:
    """Examine every candidate load and report what its feeds are not.

    Candidates are the devices in the requested scope. A device with no feeds
    at all is skipped rather than reported: it is a SOURCE, or it is on no
    circuit anyone recorded, and neither is a redundancy finding - the graph
    endpoint already says how many of those a room has.
    """
    shape, at = structural(graph)
    findings: list[Finding] = []

    for dev in sorted(candidates):
        feeds = graph.upstream.get(dev)
        if not feeds:
            continue

        by_side = graph.sides_of(dev)

        # One feed, full stop. Whether that is wrong depends on what it is,
        # which is not this service's call to make.
        if len(feeds) == 1:
            findings.append(Finding(dev, 'single_fed', sides=sorted(by_side)))
            continue

        # Several feeds, all on one side. Worse than single-fed in one respect:
        # it LOOKS redundant on a cord count, and the second cord buys nothing
        # that the first did not.
        if len(by_side) == 1 and len(feeds) > 1:
            findings.append(Finding(dev, 'same_side', sides=sorted(by_side)))
            continue

        if len(by_side) < 2:
            continue     # unsided feeds - the importer derived no side here

        # Two sides. Do they stay apart? Anything in every side's ancestry is a
        # device ALL feeds pass through: a single point of failure wearing two
        # cords. Meet points the whole estate shares are its shape, not a
        # mistake somebody made in one row.
        specific = sorted(_meet_points(graph, dev) - shape)
        if specific:
            findings.append(Finding(
                dev, 'converged', shared=specific, sides=sorted(by_side),
                shared_by=min(at[s] for s in specific)))

    return findings
