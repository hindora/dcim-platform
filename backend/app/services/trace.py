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

The walk is over SIMPLE paths: a node may not repeat within a path. Power and
cooling graphs have genuine cycles (a dual-fed rack PDU is reachable two ways,
a chilled-water loop closes on itself) and without that rule the enumeration
does not terminate.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# A trace that reaches either of these has stopped being useful as a list.
# Both are far above anything real - the deepest power chain in the reference
# estate is six hops and no load has more than two sides - so hitting one means
# the graph has a shape nobody expected, and saying so is better than returning
# ten thousand permutations of it.
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
class Path:
    """One route from the device to a source, ordered SOURCE FIRST.

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
    hops: list[Edge]


def _is_source(graph: Graph, node: str) -> bool:
    """Nothing feeds it: a utility feed, a generator, a core switch."""
    return not graph.up_of.get(node)


def trace_up(graph: Graph, device_id: str) -> tuple[list[Path], bool]:
    """Every simple path from ``device_id`` to a source.

    Returns the paths and whether the enumeration was cut short. Depth-first
    with an explicit stack rather than recursion - a cycle plus a deep chain
    is exactly the shape that blows a Python stack, and the bound has to be
    the path length rather than the interpreter's patience.
    """
    if device_id not in graph.nodes:
        return [], False

    paths: list[Path] = []
    truncated = False

    # (node, hops so far). `hops` runs device-ward -> source-ward while we
    # build it, and is reversed on completion.
    stack: list[tuple[str, list[Edge]]] = [(device_id, [])]

    while stack:
        node, hops = stack.pop()

        if len(paths) >= MAX_PATHS:
            truncated = True
            break

        feeders = graph.up_of.get(node, [])
        if not feeders:
            # A source, or - for the device itself - something nothing feeds.
            # Either way the chain ends here and there is nothing above it.
            if hops:
                paths.append(_finish(hops, "complete"))
            continue

        if len(hops) >= MAX_HOPS:
            truncated = True
            paths.append(_finish(hops, "incomplete"))
            continue

        seen = {device_id} | {h.up for h in hops} | {h.down for h in hops}
        advanced = False
        for e in feeders:
            if e.up in seen:
                # The only way back up is through somewhere we have already
                # been: a loop, not a route to a source.
                continue
            stack.append((e.up, [*hops, e]))
            advanced = True

        if not advanced and hops:
            # Every feeder above this node was already on the path. The chain
            # is real but it does not reach a source from here.
            paths.append(_finish(hops, "incomplete"))

    # Deterministic: same graph, same list, every time. Side first so A and B
    # sit together, then the shorter chain, then the id, which is stable.
    paths.sort(key=lambda p: (p.side or "~", len(p.hops),
                              p.hops[0].id if p.hops else ""))
    return paths, truncated


def _finish(hops: list[Edge], verdict: str) -> Path:
    # hops[0] is the conductor touching the traced device - the cord, the
    # patch lead - so its side is the path's side.
    side = hops[0].redundancy_side if hops else None
    return Path(side=side, verdict=verdict, hops=list(reversed(hops)))


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
    lengths = {min(v) for v in by_side.values()}
    return len(by_side) > 1 and len(lengths) > 1


def downstream(graph: Graph, device_id: str) -> list[Edge]:
    """One hop down: what is plugged into this thing.

    Not a path walk. The cascade question belongs to impact analysis, which
    answers it in the terms that matter - what goes dark, what merely loses a
    side - rather than as a list of routes.
    """
    return sorted(graph.down_of.get(device_id, []), key=lambda e: e.id)
