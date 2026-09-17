/**
 * Edge crossings on the connectivity layered layout, before and after the
 * median heuristic. Run against the live API so the numbers are the estate's
 * rather than a fixture's.
 *
 *   node tools/crossings.mjs <token> [apiBase]
 *
 * docs/24 phase 6 says to decide elkjs on the measurement rather than on
 * taste. This is the measurement.
 */
import { readFileSync } from 'node:fs';

const token = process.argv[2];
const base = process.argv[3] || 'http://127.0.0.1:8000/api/v1';
if (!token) { console.error('usage: node tools/crossings.mjs <token> [apiBase]'); process.exit(2); }

async function get(path) {
  const r = await fetch(base + path, { headers: { Authorization: `Bearer ${token}` } });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

// The layout module is TypeScript; the two functions we need are small and
// pure, so they are transliterated here rather than adding a build step to a
// diagnostic. Any drift shows up as a number that does not match the screen,
// which is the failure mode worth having.
function rankNodes(nodes, edges) {
  const ids = new Set(nodes.map((n) => n.id));
  const incoming = new Map();
  for (const e of edges) {
    if (!ids.has(e.source) || !ids.has(e.target) || e.source === e.target) continue;
    (incoming.get(e.target) ?? incoming.set(e.target, []).get(e.target)).push(e.source);
  }
  const rank = new Map(nodes.map((n) => [n.id, 0]));
  for (let pass = 0; pass < Math.min(nodes.length, 64); pass += 1) {
    let moved = false;
    for (const n of nodes) {
      const parents = incoming.get(n.id);
      if (!parents?.length) continue;
      const want = Math.max(...parents.map((p) => (rank.get(p) ?? 0) + 1));
      if (want > (rank.get(n.id) ?? 0) && want < nodes.length) { rank.set(n.id, want); moved = true; }
    }
    if (!moved) break;
  }
  return rank;
}

function collapse(edges) {
  const by = new Map();
  for (const e of edges) {
    const k = `${e.source}>${e.target}`;
    if (!by.has(k)) by.set(k, { source: e.source, target: e.target });
  }
  return [...by.values()];
}

function crossings(order, pairs) {
  const r = pairs.map(([a, b]) => [order.get(a), order.get(b)])
    .filter((p) => p[0] !== undefined && p[1] !== undefined)
    .sort((x, y) => x[0] - y[0] || x[1] - y[1]);
  let c = 0;
  for (let i = 0; i < r.length; i += 1)
    for (let j = i + 1; j < r.length; j += 1) if (r[i][1] > r[j][1]) c += 1;
  return c;
}

function total(byRank, ranks, edges) {
  const pos = new Map();
  for (const r of ranks) byRank.get(r).forEach((n, i) => pos.set(n.id, i));
  let sum = 0;
  for (let k = 1; k < ranks.length; k += 1) {
    const upper = new Set(byRank.get(ranks[k - 1]).map((n) => n.id));
    const lower = new Set(byRank.get(ranks[k]).map((n) => n.id));
    sum += crossings(pos, edges
      .filter((e) => (upper.has(e.source) && lower.has(e.target)) || (upper.has(e.target) && lower.has(e.source)))
      .map((e) => (upper.has(e.source) ? [e.source, e.target] : [e.target, e.source])));
  }
  return sum;
}

function orderRanks(byRank, ranks, edges) {
  const nb = new Map();
  const push = (a, b) => (nb.get(a) ?? nb.set(a, []).get(a)).push(b);
  for (const e of edges) { push(e.source, e.target); push(e.target, e.source); }
  const positions = () => {
    const pos = new Map();
    for (const r of ranks) byRank.get(r).forEach((n, i) => pos.set(n.id, i));
    return pos;
  };
  const sweep = (order) => {
    for (let k = 1; k < order.length; k += 1) {
      const pos = positions();
      const row = byRank.get(order[k]);
      const fixed = new Set(byRank.get(order[k - 1]).map((n) => n.id));
      const med = new Map();
      row.forEach((n, i) => {
        const ns = (nb.get(n.id) ?? []).filter((m) => fixed.has(m)).map((m) => pos.get(m)).sort((a, b) => a - b);
        med.set(n.id, ns.length ? ns[(ns.length - 1) >> 1] : i);
      });
      row.sort((a, b) => med.get(a.id) - med.get(b.id));
    }
  };
  let best = total(byRank, ranks, edges);
  let bestOrder = new Map(ranks.map((r) => [r, byRank.get(r).slice()]));
  const down = [...ranks]; const up = [...ranks].reverse();
  for (let pass = 0; pass < 4; pass += 1) {
    sweep(pass % 2 === 0 ? down : up);
    const now = total(byRank, ranks, edges);
    if (now < best) { best = now; bestOrder = new Map(ranks.map((r) => [r, byRank.get(r).slice()])); }
  }
  for (const r of ranks) byRank.set(r, bestOrder.get(r));
  return best;
}

const rooms = (await get('/rooms')).items;
/** The fewest crossings ANY ordering can achieve between two ranks that are
 *  completely connected. K(a,b) drawn in two rows has C(a,2)*C(b,2) crossings
 *  whatever order you put the rows in - a dual-homed fabric is exactly that
 *  shape, and a layout engine cannot beat arithmetic. Reported so a 0% saving
 *  can be told from a failure. */
function bipartiteFloor(byRank, ranks, edges) {
  let floor = 0;
  for (let k = 1; k < ranks.length; k += 1) {
    const upper = byRank.get(ranks[k - 1]);
    const lower = byRank.get(ranks[k]);
    const u = new Set(upper.map((n) => n.id));
    const l = new Set(lower.map((n) => n.id));
    const between = edges.filter(
      (e) => (u.has(e.source) && l.has(e.target)) || (u.has(e.target) && l.has(e.source)));
    // Only meaningful where the two ranks really are fully connected.
    if (between.length === upper.length * lower.length && upper.length > 1 && lower.length > 1) {
      const c2 = (n) => (n * (n - 1)) / 2;
      floor += c2(upper.length) * c2(lower.length);
    }
  }
  return floor;
}

console.log('room                      layer        nodes  before   after   saved  floor');
for (const room of rooms) {
  for (const layer of ['network', 'management', 'fieldbus']) {
    let g;
    try { g = await get(`/topology?layer=${layer}&scope=room:${room.id}&depth=1&rollup=rack`); }
    catch { continue; }
    if (g.node_count < 4) continue;
    const edges = collapse(g.edges);
    const rank = rankNodes(g.nodes, edges);
    const byRank = new Map();
    for (const n of g.nodes) {
      const r = rank.get(n.id) ?? 0;
      (byRank.get(r) ?? byRank.set(r, []).get(r)).push(n);
    }
    const ranks = [...byRank.keys()].sort((a, b) => a - b);
    for (const r of ranks) byRank.get(r).sort((a, b) =>
      a.device_type.localeCompare(b.device_type) || a.name.localeCompare(b.name));
    const before = total(byRank, ranks, edges);
    const after = orderRanks(byRank, ranks, edges);
    const pct = before ? Math.round((1 - after / before) * 100) : 0;
    const floor = bipartiteFloor(byRank, ranks, edges);
    const atFloor = floor > 0 && after === floor ? ' AT FLOOR' : '';
    console.log(
      `${(room.datacenter_code + ' ' + room.name).padEnd(24)} ${layer.padEnd(12)}`
      + `${String(g.node_count).padStart(5)} ${String(before).padStart(7)} `
      + `${String(after).padStart(7)} ${String(pct).padStart(5)}% ${String(floor).padStart(6)}`
      + atFloor);
  }
}
