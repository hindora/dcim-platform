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

export const NODE_W = 168;
/** Three lines: name, type, and the capacity bar. A one-line diagram without
 *  capacity on it is a picture - the number that decides anything is the live
 *  draw against what the thing is built to take. */
export const NODE_H = 56;
const GAP_X = 26;
const GAP_Y = 68;
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
    // `count` is 1 on a plain edge and the conductor count on one the server
    // already merged under a roll-up. Adding 1 per row would have reported a
    // rack's forty cords as one.
    acc.count += e.count ?? 1;
    acc.downCount += e.down_count
      || (e.oper_state === 'down' ? (e.count ?? 1) : 0);
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

/** Which distribution side a node sits on, read off the conductors touching it.
 *
 *  Derived rather than declared. A device-type table would have to be kept in
 *  step with the estate and would still be wrong about the interesting cases -
 *  the RPP that ended up feeding both sides, the load somebody corded twice to
 *  A. The edges already carry `redundancy_side`, and a node whose conductors
 *  all say A IS on the A side; one whose conductors disagree, or carry no side
 *  at all, belongs in the middle where both columns can reach it.
 */
export function sideOf(nodes: TopologyNode[], edges: CollapsedEdge[]):
    Map<string, 'A' | 'B' | null> {
  const seen = new Map<string, Set<string>>();
  const note = (id: string, sides: string[]) => {
    const set = seen.get(id) ?? seen.set(id, new Set()).get(id)!;
    for (const s of sides) set.add(s);
    if (!sides.length) set.add('-');
  };
  for (const e of edges) {
    note(e.source, e.sides);
    note(e.target, e.sides);
  }

  const out = new Map<string, 'A' | 'B' | null>();
  for (const n of nodes) {
    const set = seen.get(n.id);
    if (set?.size === 1) {
      const only = [...set][0];
      out.set(n.id, only === 'A' || only === 'B' ? only : null);
    } else {
      out.set(n.id, null);
    }
  }
  return out;
}

/** The layered layout, in two flavours.
 *
 *  `oneLine` splits each rank into an A column and a B column with the shared
 *  and dual-fed equipment down the middle - which is how every power
 *  single-line diagram in the industry is drawn, and the reason is not
 *  decoration: the question a one-line exists to answer is "is this load
 *  really fed from two places", and side-by-side is the only arrangement in
 *  which the answer is a shape rather than a reading exercise.
 *
 *  Off, it is the plain centred rank, which is right for a fabric where the
 *  concept does not apply.
 */
export function layout(nodes: TopologyNode[], edges: CollapsedEdge[],
                       opts: { oneLine?: boolean } = {}): {
  placed: Placed[];
  width: number;
  height: number;
} {
  if (!nodes.length) return { placed: [], width: 0, height: 0 };

  const rank = rankNodes(nodes, edges);
  const side = opts.oneLine ? sideOf(nodes, edges) : null;

  const byRank = new Map<number, TopologyNode[]>();
  for (const n of nodes) {
    const r = rank.get(n.id) ?? 0;
    (byRank.get(r) ?? byRank.set(r, []).get(r)!).push(n);
  }

  // Nothing carries a side - an estate the importer never derived redundancy
  // for, or a layer where the idea does not exist. Two empty columns around a
  // full middle is a worse picture than the centred rank, so fall back.
  const anySided = side ? [...side.values()].some(Boolean) : false;
  const columns = Boolean(side && anySided);

  const placed: Placed[] = [];
  let y = 0;
  let width = 0;
  const step = NODE_W + GAP_X;

  for (const r of [...byRank.keys()].sort((a, b) => a - b)) {
    // Deterministic order within a rank: same graph, same picture, every time.
    const row = byRank.get(r)!.slice().sort((a, b) =>
      a.device_type.localeCompare(b.device_type) || a.name.localeCompare(b.name));

    if (!columns) {
      const lines = Math.ceil(row.length / MAX_PER_LINE);
      for (let i = 0; i < row.length; i += 1) {
        const line = Math.floor(i / MAX_PER_LINE);
        const col = i % MAX_PER_LINE;
        const inLine = Math.min(MAX_PER_LINE, row.length - line * MAX_PER_LINE);
        // Centre each line so a short rank sits under the middle of a wide one.
        const lineWidth = inLine * step;
        placed.push({
          node: row[i],
          x: col * step - lineWidth / 2,
          y: y + line * (NODE_H + 10),
        });
        width = Math.max(width, lineWidth);
      }
      y += lines * (NODE_H + 10) + GAP_Y;
      continue;
    }

    const groups = {
      A: row.filter((n) => side!.get(n.id) === 'A'),
      M: row.filter((n) => side!.get(n.id) === null),
      B: row.filter((n) => side!.get(n.id) === 'B'),
    };

    // Each column wraps on its own, so one wide rank of loads does not force
    // the columns apart for the whole diagram.
    const perCol = Math.max(1, Math.floor(MAX_PER_LINE / 2));
    const linesIn = (n: number) => Math.max(1, Math.ceil(n / perCol));
    const lines = Math.max(linesIn(groups.A.length), linesIn(groups.M.length),
                           linesIn(groups.B.length));

    // A grows leftwards from the centre gutter, B rightwards, the middle
    // straddles it. Widths are per-rank so a rank with no B side does not
    // leave a hole where its column would have been.
    const half = (g: TopologyNode[]) => Math.min(perCol, g.length) * step;
    const gutter = Math.max(half(groups.M), step) / 2 + GAP_X;

    const place = (g: TopologyNode[], originX: number, dir: 1 | -1) => {
      for (let i = 0; i < g.length; i += 1) {
        const line = Math.floor(i / perCol);
        const col = i % perCol;
        placed.push({
          node: g[i],
          x: dir === 1
            ? originX + col * step
            : originX - (col + 1) * step,
          y: y + line * (NODE_H + 10),
        });
      }
    };

    place(groups.A, -gutter, -1);
    place(groups.B, gutter, 1);
    // The middle is centred on the gutter itself, which is where a dual-corded
    // load belongs: one cord reaching left, one reaching right.
    const mWidth = Math.min(perCol, groups.M.length) * step;
    place(groups.M, -mWidth / 2, 1);

    width = Math.max(width,
      2 * (gutter + Math.max(half(groups.A), half(groups.B))));
    y += lines * (NODE_H + 10) + GAP_Y;
  }

  return { placed, width: width + NODE_W, height: y };
}
