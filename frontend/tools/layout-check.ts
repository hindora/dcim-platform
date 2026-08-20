/* Headless check of the exit criterion for phase 4.6: live state must not
 * re-lay the graph out.
 *
 * Outside src/, so it is neither type-checked with the app nor bundled. Run it
 * against a real API response:
 *
 *   ./node_modules/.bin/esbuild tools/layout-check.ts --bundle --platform=node  *       --format=cjs --outfile=/tmp/lc.cjs
 *   curl -s -H "Authorization: Bearer $TOKEN"  *       "$API/topology?layer=power&scope=room:$ROOM&depth=1" -o /tmp/g.json
 *   node /tmp/lc.cjs /tmp/g.json
 */
import { collapseEdges, layout, structureKey } from '../src/features/topology/layout';
import type { TopologyEdge, TopologyNode } from '../src/api/client';

// Declared rather than pulling in @types/node: this is the only Node file in a
// browser project, and it lives outside tsconfig's `include` so the app build
// never sees it.
declare const require: (m: string) => { readFileSync(p: string, e: string): string };
declare const process: { argv: string[] };

// Read from a file: a room-scoped power graph is ~650 KB, past the argv limit.
const raw = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8')) as {
  nodes: TopologyNode[]; edges: TopologyEdge[]; node_count: number; edge_count: number;
};

const nodes = raw.nodes;
const edges = raw.edges;
const collapsed = collapseEdges(edges);

const key1 = structureKey(nodes, edges);
const place1 = layout(nodes, collapsed);

// A live update: every status and metric changes, the graph does not.
const churned: TopologyNode[] = nodes.map((n) => ({
  ...n,
  status: n.status === 'ONLINE' ? 'OFFLINE' : 'ONLINE',
  max_severity: 'CRITICAL',
  metrics: { power_w: Math.random() * 1000 },
}));
const key2 = structureKey(churned, edges);
const place2 = layout(churned, collapseEdges(edges));

const posOf = (p: ReturnType<typeof layout>) =>
  JSON.stringify(p.placed.map((x) => [x.node.id, x.x, x.y]).sort());

const sameKey = key1 === key2;
const samePos = posOf(place1) === posOf(place2);

// And a genuine structural change MUST produce a new key, or the cache would
// pin a stale layout over a graph that really did change.
const fewer = nodes.slice(0, Math.max(1, nodes.length - 1));
const key3 = structureKey(fewer, edges.filter(
  (e) => fewer.some((n) => n.id === e.source) && fewer.some((n) => n.id === e.target)));

const ranks = new Set(place1.placed.map((p) => p.y));
console.log(JSON.stringify({
  nodes: nodes.length,
  edges: edges.length,
  collapsed: collapsed.length,
  ranks: ranks.size,
  width: Math.round(place1.width),
  height: Math.round(place1.height),
  structure_key_stable_under_state_change: sameKey,
  positions_identical_under_state_change: samePos,
  structure_key_changes_when_graph_changes: key1 !== key3,
  overlapping_nodes: place1.placed.length - new Set(
    place1.placed.map((p) => `${p.x},${p.y}`)).size,
}, null, 1));
