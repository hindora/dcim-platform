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
            # Report the ones nearest the load first: the closer a convergence
            # is, the more of the chain it takes with it.
            findings.append(Finding(
                dev, 'converged',
                shared=sorted(shared),
                sides=sorted(by_side)))

    return findings
