"""Impact analysis: what breaks if this device is taken out.

Asked before every maintenance window - pulling a UPS for a battery change,
dropping a PDU for breaker work, rebooting an OOB switch - and the answer an
operator needs is not "these 300 things are downstream". It is the much shorter
list of things that would actually go dark, separated from the things that
would merely stop being redundant.

The distinction is the whole product. Taking UPSA out drops every A-side load
to a single feed, which is usually an accepted risk for a planned window. But a
single-corded load hanging off the A side goes DARK, and that is the list
someone has to act on beforehand.

Method: a load is still served if a path from a SOURCE reaches it without
passing through the candidate. Not "does it have another feeder" - that feeder
may itself be fed only through the candidate, and answering one hop deep misses
the cascade entirely.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

# What losing the last upstream path means on each layer. The verdict is only
# as strong as the layer's semantics, and saying so is part of the answer.
LAYER_EFFECT = {
    "power": "loses power",
    "cooling": "loses cooling supply",
    "management": "loses monitoring",
    "fieldbus": "loses monitoring",
    "production": "loses network path",
}

# Layers where losing a path is a real service loss rather than a topology
# curiosity. Ordered for presentation: power first, it is what gets someone
# out of bed.
LAYERS = ("power", "cooling", "management", "fieldbus", "production")


@dataclass
class Graph:
    """A single layer, normalised so edges always run upstream -> downstream."""

    downstream: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    upstream: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    # (downstream device) -> {redundancy side of each feed into it}
    sides: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    # (downstream, upstream) -> side, so a side can be recomputed after a cut
    edge_side: dict[tuple[str, str], str] = field(default_factory=dict)
    nodes: set[str] = field(default_factory=set)

    def add(self, up: str, down: str, side: str | None) -> None:
        self.downstream[up].add(down)
        self.upstream[down].add(up)
        self.nodes.add(up)
        self.nodes.add(down)
        if side:
            self.sides[down].add(side)
            self.edge_side[(down, up)] = side

    def sources(self) -> set[str]:
        """Nodes nothing feeds: utility feeds, generators, core switches."""
        return {n for n in self.nodes if not self.upstream.get(n)}


def _reachable(graph: Graph, start: set[str], *, without: str | None) -> set[str]:
    seen: set[str] = set()
    stack = [n for n in start if n != without]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for nxt in graph.downstream.get(node, ()):
            if nxt != without and nxt not in seen:
                stack.append(nxt)
    return seen


@dataclass
class LayerImpact:
    layer: str
    effect: str
    dependents: set[str]
    cut_off: set[str]
    degraded: set[str]


def analyse(graph: Graph, device_id: str, layer: str) -> LayerImpact:
    """Impact of removing ``device_id`` from one layer.

    cut_off  - downstream devices with no surviving path from any source.
    degraded - downstream devices still served, but by fewer distinct
               redundancy sides than before. Power only; the other layers have
               no equivalent notion of a labelled second path.
    """
    dependents = _reachable(graph, {device_id}, without=None) - {device_id}
    if not dependents:
        return LayerImpact(layer, LAYER_EFFECT[layer], set(), set(), set())

    # Everything a source can still reach with the candidate removed.
    alive = _reachable(graph, graph.sources(), without=device_id)

    # A source has no upstream by definition, so removing the candidate cannot
    # cut one off; but a source is never "downstream" of anything either, so
    # this only matters for the degenerate case of the candidate itself.
    cut_off = {d for d in dependents if d not in alive}

    degraded: set[str] = set()
    if layer == "power":
        for dev in dependents - cut_off:
            before = graph.sides.get(dev, set())
            if len(before) < 2:
                continue        # nothing to lose; it was single-fed already
            after = {side for up, side in
                     ((u, graph.edge_side.get((dev, u))) for u in graph.upstream[dev])
                     if side and up in alive}
            if after < before:
                degraded.add(dev)

    return LayerImpact(layer, LAYER_EFFECT[layer], dependents, cut_off, degraded)
