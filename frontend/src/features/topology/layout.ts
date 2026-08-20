import type { TopologyEdge, TopologyNode } from '../../api/client';

/** A layered layout, computed purely from graph STRUCTURE.
 *
 *  Not force-directed, for two reasons.
 *
 *  The domain one: infrastructure graphs are hierarchical. A power chain is a
 *  one-line diagram — utility at the top, load at the bottom — and that is how
 *  every real DCIM tool draws it. A force simulation would scatter that into a
 *  blob with no reading order.
 *
 *  The harder one: the requirement is that live updates must not re-lay the
 *  graph out. A force simulation re-converges (and jitters) whenever it is
 *  nudged, so "don't move on update" becomes a fight against the algorithm.
 *  A pure function of structure cannot move unless the structure moves, which
 *  makes the requirement a property of the design rather than a patch on it.
 */

export interface Placed {
  node: TopologyNode;
  x: number;
  y: number;
}

export interface CollapsedEdge {
  source: string;
  target: string;
  /** How many parallel connections were collapsed into this one line.
   *  The graph carries one edge per conductor — seven between a UPS and an
   *  RPP — which render as one indistinguishable line. Collapsing and counting
   *  keeps the fact without drawing it seven times. */
  count: number;
  downCount: number;
  sides: string[];
}

export const NODE_W = 132;
export const NODE_H = 30;
const GAP_X = 14;
const GAP_Y = 60;
/** Widest a rank may get before it wraps onto another line. A rank holding a
 *  hall's worth of servers is 140 wide; strung out in one row it would be
 *  13000 px and unreadable. */
const MAX_PER_LINE = 12;

/** Identity of the STRUCTURE, ignoring anything that changes with live state.
 *  Positions are memoised on this, so status and metric updates cannot move a
 *  node — only a genuinely different graph can. */
export function structureKey(nodes: TopologyNode[], edges: TopologyEdge[]): string {
  const n = nodes.map((x) => x.id).sort().join(',');
  const e = edges.map((x) => `${x.source}>${x.target}`).sort().join(',');
  return `${n}|${e}`;
}

export function collapseEdges(edges: TopologyEdge[]): CollapsedEdge[] {
  const by = new Map<string, CollapsedEdge>();
  for (const e of edges) {
    const key = `${e.source}>${e.target}`;
    let acc = by.get(key);
    if (!acc) {
      acc = { source: e.source, target: e.target, count: 0, downCount: 0, sides: [] };
      by.set(key, acc);
    }
    acc.count += 1;
    if (e.oper_state === 'down') acc.downCount += 1;
    if (e.redundancy_side && !acc.sides.includes(e.redundancy_side)) {
      acc.sides.push(e.redundancy_side);
    }
  }
  return [...by.values()];
}

/** Rank each node by its longest path from a root.
 *
 *  Longest rather than shortest: a PDU fed directly by an RPP and also, via a
 *  longer route, by something upstream of it must sit BELOW both, or its edges
 *  point backwards up the diagram.
 *
 *  Cycles are real on the ethernet layers (switch to switch), so the walk is
 *  bounded by the node count instead of assuming a DAG.
 */
function rankNodes(nodes: TopologyNode[], edges: CollapsedEdge[]): Map<string, number> {
  const ids = new Set(nodes.map((n) => n.id));
  const incoming = new Map<string, string[]>();
  const outgoing = new Map<string, string[]>();
  for (const e of edges) {
    if (!ids.has(e.source) || !ids.has(e.target) || e.source === e.target) continue;
    (incoming.get(e.target) ?? incoming.set(e.target, []).get(e.target)!).push(e.source);
    (outgoing.get(e.source) ?? outgoing.set(e.source, []).get(e.source)!).push(e.target);
  }

  const rank = new Map<string, number>();
  for (const n of nodes) rank.set(n.id, 0);

  // Relax until stable. Bounded by node count so a cycle terminates instead of
  // spinning: within a cycle the ranks simply stop improving.
  for (let pass = 0; pass < Math.min(nodes.length, 64); pass += 1) {
    let moved = false;
    for (const n of nodes) {
      const parents = incoming.get(n.id);
      if (!parents?.length) continue;
      const want = Math.max(...parents.map((p) => (rank.get(p) ?? 0) + 1));
      if (want > (rank.get(n.id) ?? 0) && want < nodes.length) {
        rank.set(n.id, want);
        moved = true;
      }
    }
    if (!moved) break;
  }
  return rank;
}

export function layout(nodes: TopologyNode[], edges: CollapsedEdge[]): {
  placed: Placed[];
  width: number;
  height: number;
} {
  if (!nodes.length) return { placed: [], width: 0, height: 0 };

  const rank = rankNodes(nodes, edges);

  const byRank = new Map<number, TopologyNode[]>();
  for (const n of nodes) {
    const r = rank.get(n.id) ?? 0;
    (byRank.get(r) ?? byRank.set(r, []).get(r)!).push(n);
  }

  const placed: Placed[] = [];
  let y = 0;
  let width = 0;
  for (const r of [...byRank.keys()].sort((a, b) => a - b)) {
    // Deterministic order within a rank: same graph, same picture, every time.
    const row = byRank.get(r)!.slice().sort((a, b) =>
      a.device_type.localeCompare(b.device_type) || a.name.localeCompare(b.name));

    const lines = Math.ceil(row.length / MAX_PER_LINE);
    for (let i = 0; i < row.length; i += 1) {
      const line = Math.floor(i / MAX_PER_LINE);
      const col = i % MAX_PER_LINE;
      const inLine = Math.min(MAX_PER_LINE, row.length - line * MAX_PER_LINE);
      // Centre each line so a short rank sits under the middle of a wide one.
      const lineWidth = inLine * (NODE_W + GAP_X);
      placed.push({
        node: row[i],
        x: col * (NODE_W + GAP_X) - lineWidth / 2,
        y: y + line * (NODE_H + 10),
      });
      width = Math.max(width, lineWidth);
    }
    y += lines * (NODE_H + 10) + GAP_Y;
  }

  return { placed, width: width + NODE_W, height: y };
}
