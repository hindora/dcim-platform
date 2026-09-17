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

/** Wide enough for a full device name at 9px mono, and no wider.
 *
 *  These names end in the part that distinguishes them - R1-04 from R2-01 -
 *  so an ellipsis eats the only characters that matter. 104px cut
 *  "PDUA-DC1-HA-R2-01" by two characters and made a dozen boxes read alike. */
export const NODE_W = 120;
/** Three lines: name, type, and the capacity bar. A one-line diagram without
 *  capacity on it is a picture - the number that decides anything is the live
 *  draw against what the thing is built to take. */
export const NODE_H = 48;
const GAP_X = 22;
const GAP_Y = 56;
/** Widest a rank may get before it wraps onto another line. A rank holding a
 *  hall's worth of servers is 140 wide; strung out in one row it would be
 *  13000 px and unreadable. */
const MAX_PER_LINE = 12;
/** The loop layout wraps its middle narrower: see `layoutLoop`. */
const LOOP_PER_LINE = 6;

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

/** Count the edge crossings between two adjacent ranks, given an ordering.
 *
 *  Two edges cross when one starts left of the other and ends right of it.
 *  Exported so the readability of a change can be MEASURED rather than argued
 *  about: `tools/crossings.mjs` prints the count before and after.
 */
export function countCrossings(order: Map<string, number>,
                               pairs: [string, string][]): number {
  const ranked = pairs
    .map(([a, b]) => [order.get(a), order.get(b)] as [number?, number?])
    .filter((p): p is [number, number] => p[0] !== undefined && p[1] !== undefined)
    .sort((x, y) => x[0] - y[0] || x[1] - y[1]);
  let crossings = 0;
  for (let i = 0; i < ranked.length; i += 1) {
    for (let j = i + 1; j < ranked.length; j += 1) {
      if (ranked[i][1] > ranked[j][1]) crossings += 1;
    }
  }
  return crossings;
}

/** Order each rank so its edges cross as little as possible.
 *
 *  The median heuristic, from the Sugiyama framework: put a node where the
 *  middle of its neighbours in the previous rank is. Sweeping down then up and
 *  keeping whichever pass was better is the standard refinement, and it is
 *  most of what a full layout engine buys on a graph this size - which is why
 *  elkjs is still unadopted and still carries a licence question.
 *
 *  A node with no neighbours in the reference rank keeps its current place
 *  rather than collapsing to zero: it has no opinion, and giving it one drags
 *  everything with an opinion out of position.
 */
function orderRanks(byRank: Map<number, TopologyNode[]>, ranks: number[],
                    edges: CollapsedEdge[]): void {
  const neighbours = new Map<string, string[]>();
  const push = (a: string, b: string) =>
    (neighbours.get(a) ?? neighbours.set(a, []).get(a)!).push(b);
  for (const e of edges) { push(e.source, e.target); push(e.target, e.source); }

  const positions = () => {
    const pos = new Map<string, number>();
    for (const r of ranks) byRank.get(r)!.forEach((n, i) => pos.set(n.id, i));
    return pos;
  };

  const sweep = (order: number[]) => {
    for (let k = 1; k < order.length; k += 1) {
      const pos = positions();
      const row = byRank.get(order[k])!;
      const fixed = new Set(byRank.get(order[k - 1])!.map((n) => n.id));
      const median = new Map<string, number>();
      row.forEach((n, i) => {
        const ns = (neighbours.get(n.id) ?? [])
          .filter((m) => fixed.has(m))
          .map((m) => pos.get(m)!)
          .sort((a, b) => a - b);
        // No opinion: keep the place it already has.
        median.set(n.id, ns.length ? ns[(ns.length - 1) >> 1] : i);
      });
      row.sort((a, b) => median.get(a.id)! - median.get(b.id)!);
    }
  };

  const total = () => {
    const pos = positions();
    let sum = 0;
    for (let k = 1; k < ranks.length; k += 1) {
      const upper = new Set(byRank.get(ranks[k - 1])!.map((n) => n.id));
      const lower = new Set(byRank.get(ranks[k])!.map((n) => n.id));
      sum += countCrossings(pos, edges
        .filter((e) => (upper.has(e.source) && lower.has(e.target))
                    || (upper.has(e.target) && lower.has(e.source)))
        .map((e) => (upper.has(e.source) ? [e.source, e.target] : [e.target, e.source])));
    }
    return sum;
  };

  let best = total();
  let bestOrder = new Map(ranks.map((r) => [r, byRank.get(r)!.slice()]));
  const down = [...ranks];
  const up = [...ranks].reverse();
  // Four passes. The heuristic converges fast and a fifth has never moved the
  // number on anything in this estate.
  for (let pass = 0; pass < 4; pass += 1) {
    sweep(pass % 2 === 0 ? down : up);
    const now = total();
    if (now < best) {
      best = now;
      bestOrder = new Map(ranks.map((r) => [r, byRank.get(r)!.slice()]));
    }
  }
  for (const r of ranks) byRank.set(r, bestOrder.get(r)!);
}

/** A cooling circuit drawn as one: supply down the left, the units it serves
 *  across the bottom, the return back up the right.
 *
 *  Chilled water is a CYCLE, and the layered rank strings it out as a ladder
 *  with the return header stranded at the bottom as though it were another
 *  load. It is not a load - it is the same pipe coming back, and an operator
 *  reading a cooling diagram is looking for a circuit. Bending the ranks
 *  around a U puts the supply header and the return header next to each other
 *  at the top, where the loop visibly closes.
 *
 *  Structural, not a device-type template. The plan called for a fixed stage
 *  list - tower, condenser, chiller, primary pump, secondary pump, header,
 *  terminal - and that is a table to keep in step with every estate that ever
 *  differs from this one. The shape is already in the graph: the widest rank
 *  is where the plant fans out into the units it serves, everything above it
 *  is supply and everything below it is return.
 *
 *  Returns null when the graph is not that shape - too few ranks, or the
 *  widest rank at one end, which is a tree and not a circuit. The caller falls
 *  back to the layered layout rather than bending a ladder into a U and
 *  claiming it is a loop.
 */
function layoutLoop(nodes: TopologyNode[], edges: CollapsedEdge[]): {
  placed: Placed[]; width: number; height: number;
} | null {
  const ids = new Set(nodes.map((n) => n.id));
  const down = new Map<string, string[]>();
  const up = new Map<string, string[]>();
  for (const e of edges) {
    if (!ids.has(e.source) || !ids.has(e.target) || e.source === e.target) continue;
    (down.get(e.source) ?? down.set(e.source, []).get(e.source)!).push(e.target);
    (up.get(e.target) ?? up.set(e.target, []).get(e.target)!).push(e.source);
  }

  const sources = nodes.filter((n) => !up.get(n.id)?.length).map((n) => n.id);
  const sinks = nodes.filter((n) => !down.get(n.id)?.length).map((n) => n.id);
  if (!sources.length || !sinks.length) return null;

  /** Hops to the nearest source, and to the nearest sink, both following the
   *  flow. Breadth-first and visited-guarded: the graph really is a cycle. */
  const bfs = (from: string[], edgesOf: Map<string, string[]>) => {
    const d = new Map<string, number>(from.map((i) => [i, 0]));
    const queue = [...from];
    for (let i = 0; i < queue.length; i += 1) {
      const cur = queue[i];
      for (const nxt of edgesOf.get(cur) ?? []) {
        if (!d.has(nxt)) { d.set(nxt, d.get(cur)! + 1); queue.push(nxt); }
      }
    }
    return d;
  };
  const dSource = bfs(sources, down);
  const dSink = bfs(sinks, up);

  // Nearer the source than the sink is supply; nearer the sink is return;
  // equidistant is where the water does its work. That last one is the real
  // definition of a terminal in a circuit and it needs no device-type table:
  // a CRAH is one hop from the supply header and one hop from the return.
  const supply: TopologyNode[] = [];
  const middle: TopologyNode[] = [];
  const ret: TopologyNode[] = [];
  for (const n of nodes) {
    const a = dSource.get(n.id);
    const b = dSink.get(n.id);
    if (a === undefined || b === undefined) { middle.push(n); continue; }
    if (a < b) supply.push(n);
    else if (b < a) ret.push(n);
    else middle.push(n);
  }
  if (!supply.length || !ret.length || !middle.length) return null;

  const step = NODE_W + GAP_X;
  const rowH = NODE_H + GAP_Y;
  const byName = (a: TopologyNode, b: TopologyNode) =>
    a.device_type.localeCompare(b.device_type) || a.name.localeCompare(b.name);

  // Each leg is banded by its distance, so a chain of plant reads down the
  // page in the order the water goes through it.
  const band = (list: TopologyNode[], dist: Map<string, number>) => {
    const out = new Map<number, TopologyNode[]>();
    for (const n of list) {
      const d = dist.get(n.id) ?? 0;
      (out.get(d) ?? out.set(d, []).get(d)!).push(n);
    }
    return [...out.entries()].sort((x, y) => x[0] - y[0])
      .map(([, v]) => v.slice().sort(byName));
  };
  const supplyBands = band(supply, dSource);
  const retBands = band(ret, dSink);

  // The middle keeps its own layering so a CDU sits above the servers it
  // serves rather than beside them.
  const midRank = rankNodes(middle, edges.filter(
    (e) => middle.some((m) => m.id === e.source) && middle.some((m) => m.id === e.target)));
  const midBands = band(middle, midRank);

  // Narrower than the layered layout wraps at. A U is as wide as its middle
  // plus two legs, and wrapping twenty-one terminals in one row of twelve put
  // the supply header fifteen hundred pixels from the return header with
  // nothing between them - a circuit nobody can see both ends of. Six keeps
  // the whole loop roughly square, which is the shape that reads as a loop.
  const perLine = Math.min(LOOP_PER_LINE, Math.max(...midBands.map((b) => b.length)));
  const midWidth = perLine * step;

  // One step clear of the middle, so a pipe never runs under a unit it does
  // not serve.
  const legX = midWidth / 2 + step * 0.7;

  const placed: Placed[] = [];

  // A leg band wraps like the middle does. A scoped view can put a lot on one
  // leg - the plant room's cooling graph is cut off before its return header,
  // so every CRAH it feeds is a sink and lands on the return leg - and a band
  // of fourteen strung out in one row is a mile of canvas with a hairball of
  // pipe across the top of it.
  const leg = (bands: TopologyNode[][]) => {
    let cursor = 0;
    const rows: { row: TopologyNode[]; line: number }[] = [];
    for (const band of bands) {
      const lines = Math.ceil(band.length / LOOP_PER_LINE);
      for (let l = 0; l < lines; l += 1) {
        rows.push({
          row: band.slice(l * LOOP_PER_LINE, (l + 1) * LOOP_PER_LINE),
          line: cursor,
        });
        cursor += 1;
      }
    }
    return { rows, height: cursor };
  };

  const supplyLeg = leg(supplyBands);
  const retLeg = leg(retBands);
  const legRows = Math.max(supplyLeg.height, retLeg.height);

  supplyLeg.rows.forEach(({ row, line }) => row.forEach((n, j) => placed.push({
    node: n, x: -legX - (row.length - 1 - j) * step, y: line * rowH,
  })));
  // Ascending: the band nearest the units sits at the BOTTOM of the right-hand
  // leg and the sink at the top, so the water reads as coming back to where it
  // started.
  retLeg.rows.forEach(({ row, line }) => row.forEach((n, j) => placed.push({
    node: n, x: legX + j * step, y: (legRows - 1 - line) * rowH,
  })));

  let y = legRows * rowH;
  midBands.forEach((row) => {
    const lines = Math.ceil(row.length / perLine);
    row.forEach((n, i) => {
      const line = Math.floor(i / perLine);
      const col = i % perLine;
      const inLine = Math.min(perLine, row.length - line * perLine);
      placed.push({
        node: n, x: col * step - (inLine * step) / 2,
        y: y + line * (NODE_H + 12),
      });
    });
    y += lines * (NODE_H + 12) + GAP_Y * 0.55;
  });

  return { placed, width: 2 * legX + midWidth, height: y };
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
                       opts: { oneLine?: boolean; loop?: boolean } = {}): {
  placed: Placed[];
  width: number;
  height: number;
} {
  if (!nodes.length) return { placed: [], width: 0, height: 0 };

  if (opts.loop) {
    const circuit = layoutLoop(nodes, edges);
    if (circuit) return circuit;
    // Not a circuit shape. Fall through rather than bend a ladder into a U.
  }

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

  const ranks = [...byRank.keys()].sort((a, b) => a - b);

  // Start from a deterministic order - same graph, same picture, every time -
  // then let the median heuristic reduce the crossings from there. Seeding it
  // rather than leaving the insertion order matters: the heuristic keeps a
  // node with no neighbours in the reference rank where it already is, so the
  // seed is what those nodes end up sorted by.
  for (const r of ranks) {
    byRank.get(r)!.sort((a, b) =>
      a.device_type.localeCompare(b.device_type) || a.name.localeCompare(b.name));
  }
  // Only on the plain layered picture. The one-line's order within a rank is
  // its SIDE - A left, B right - and re-ordering to unpick crossings would
  // move a feeder into the wrong column, which is a worse lie than a crossing.
  if (!columns) orderRanks(byRank, ranks, edges);

  const placed: Placed[] = [];
  let y = 0;
  let width = 0;
  const step = NODE_W + GAP_X;

  for (const r of ranks) {
    const row = byRank.get(r)!;

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
