"""Cable trace: the chain from one device back to its source.

The question is older than the software. Someone is standing in front of a
rack about to unplug a cord, and what they need is the list of what it passes
through on the way to the thing that generates the power - which outlet, which
breaker branch, which RPP, which UPS - because the decision they are about to
make depends on the far end, not the near one.

Upstream only, deliberately. "What hangs off this PDU" is a different question
with a much better answer already built: ``services/impact`` returns the set
that goes dark and the set that merely loses a side, which is what anyone
asking downstream actually wants. Enumerating downstream paths would produce
four hundred near-identical lists and answer neither question. The one
downstream fact worth carrying here is the immediate neighbours - what is
plugged into this thing - and that is one hop, not a path.

ONE PATH PER CORD, not one per route. This is the whole shape of the thing and
it was got wrong first: enumerating every root-to-device route multiplies out
every fork ABOVE the device, and in a normal 2N estate that is a lot of forks.
A dual-corded server behind a switchgear pair with a utility and two
generators came back as SIX six-hop paths, identical from the ATS down and
differing only in which source sat at the top. Nobody can read that, and it
answers a question nobody asked - the operator has two cords, so there are two
chains, and the alternative sources are a property of one hop in each.

So the walk follows each cord upward, taking one feeder at a time, and where a
node has more than one feeder it records the others as ALTERNATES on that hop
and keeps going. That is also what NetBox does when a cable trace forks, and
for the same reason: a trace that guesses silently is worse than one that says
where it chose.

The walk will not revisit a node within a path. Power and cooling graphs have
genuine cycles - a closed tie breaker, a chilled-water loop - and without that
rule it does not terminate.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# One path per cord, so the bound is on how many cords one device can have.
# Both are far above anything real - the deepest power chain in the reference
# estate is six hops and no load has more than two cords - so hitting one means
# the graph has a shape nobody expected, and saying so is better than printing
# it.
MAX_PATHS = 24
MAX_HOPS = 24


@dataclass(frozen=True)
class Edge:
    """One conductor, oriented so ``up`` feeds ``down``."""

    id: str
    up: str
    down: str
    link_type: str | None
    redundancy_side: str | None
    oper_state: str
    up_termination_type: str
    up_termination_id: str | None
    down_termination_type: str
    down_termination_id: str | None


@dataclass
class Graph:
    """One layer, indexed both ways."""

    up_of: dict[str, list[Edge]] = field(default_factory=lambda: defaultdict(list))
    down_of: dict[str, list[Edge]] = field(default_factory=lambda: defaultdict(list))
    nodes: set[str] = field(default_factory=set)

    def add(self, e: Edge) -> None:
        self.up_of[e.down].append(e)
        self.down_of[e.up].append(e)
        self.nodes.add(e.up)
        self.nodes.add(e.down)


def build(rows: list[dict[str, Any]]) -> Graph:
    g = Graph()
    for r in rows:
        g.add(Edge(
            id=r["id"], up=r["up"], down=r["down"],
            link_type=r.get("link_type"),
            redundancy_side=r.get("redundancy_side"),
            oper_state=r.get("oper_state") or "unknown",
            up_termination_type=r.get("up_termination_type") or "none",
            up_termination_id=r.get("up_termination_id"),
            down_termination_type=r.get("down_termination_type") or "none",
            down_termination_id=r.get("down_termination_id"),
        ))
    return g


@dataclass
class Hop:
    """One conductor on a path, plus the feeders the walk did not take."""

    edge: Edge
    # Other devices feeding ``edge.down`` at this point. A generator beside a
    # utility on a switchgear board, the second core beside the first. Empty on
    # a chain with one way up, which is most of them.
    alternates: list[str] = field(default_factory=list)


@dataclass
class Path:
    """One cord's chain to a source, ordered SOURCE FIRST.

    Source first because that is the reading order of every one-line diagram
    ever drawn, and the table is the same trace in another form - a reader who
    has learnt to start at the top of the picture must not have to start at the
    bottom of the list.
    """

    # 'A' or 'B', taken from the conductor nearest the traced device: that is
    # the cord someone would actually pull, and it is the side the rest of the
    # chain inherits.
    side: str | None
    verdict: str
    hops: list[Hop]


def trace_up(graph: Graph, device_id: str) -> tuple[list[Path], bool]:
    """One chain per cord, walked up to a source.

    Returns the paths and whether there were more cords than the bound allows.
    Iterative rather than recursive: a deep chain plus a cycle is exactly the
    shape that blows a Python stack, and the bound should be the hop count
    rather than the interpreter's patience.
    """
    if device_id not in graph.nodes:
        return [], False

    cords = sorted(graph.up_of.get(device_id, []),
                   key=lambda e: (e.redundancy_side or "~", e.id))
    truncated = len(cords) > MAX_PATHS

    paths: list[Path] = []
    for cord in cords[:MAX_PATHS]:
        hops = [Hop(cord)]
        seen = {device_id, cord.up}
        node = cord.up
        verdict = "complete"

        while True:
            feeders = [e for e in graph.up_of.get(node, []) if e.up not in seen]
            if not graph.up_of.get(node):
                break                       # a source: nothing feeds it
            if not feeders:
                # Everything above is already on this path. The chain is real
                # but it closes on itself instead of reaching a source.
                verdict = "incomplete"
                break
            if len(hops) >= MAX_HOPS:
                verdict = "incomplete"
                break

            # Stay on the side we started on where the graph offers a choice -
            # an A cord traced up through the B board would be a fiction. Then
            # by id, so the same graph gives the same chain every time.
            chosen = min(feeders,
                         key=lambda e: (e.redundancy_side != cord.redundancy_side,
                                        e.id))
            hops.append(Hop(chosen, [e.up for e in feeders if e is not chosen]))
            seen.add(chosen.up)
            node = chosen.up

        paths.append(Path(side=cord.redundancy_side, verdict=verdict,
                          hops=list(reversed(hops))))

    return paths, truncated


def is_asymmetric(paths: list[Path]) -> bool:
    """Do the labelled sides disagree about how far the source is?

    Two feeds that take a different number of hops are not automatically
    wrong, but they are worth saying out loud: it is the signature of a B side
    that was extended through an extra RPP during a build and never squared up,
    and of a path that was traced to a panel on one side and to the utility on
    the other.
    """
    by_side: dict[str, set[int]] = defaultdict(set)
    for p in paths:
        if p.side:
            by_side[p.side].add(len(p.hops))
    if len(by_side) < 2:
        return False
    return len({min(v) for v in by_side.values()}) > 1


def downstream(graph: Graph, device_id: str) -> list[Edge]:
    """One hop down: what is plugged into this thing.

    Not a path walk. The cascade question belongs to impact analysis, which
    answers it in the terms that matter - what goes dark, what merely loses a
    side - rather than as a list of routes.
    """
    return sorted(graph.down_of.get(device_id, []), key=lambda e: e.id)
