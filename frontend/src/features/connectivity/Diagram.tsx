import { useChartColors } from '../../components/seriesColors';
import {
  NODE_H, NODE_W,
  type CollapsedEdge, type Placed,
} from './layout';
import type { TopologyNode } from '../../api/client';

/** The connectivity diagram.
 *
 *  Rendering only - placement is computed by the page and handed in, so the
 *  panel and its maximized twin draw the identical picture from one layout
 *  rather than laying the graph out twice and disagreeing about it.
 */

export interface Placement {
  placed: Placed[];
  width: number;
  height: number;
}

function statusFill(status: string, severity: string): string {
  if (status === 'OFFLINE') return 'var(--critical)';
  if (status === 'UNKNOWN') return 'var(--unknown)';
  switch (severity) {
    case 'CRITICAL': return 'var(--critical)';
    case 'MAJOR': return 'var(--major)';
    case 'MINOR':
    case 'WARNING': return 'var(--warn)';
    default: return 'var(--ok)';
  }
}

/** A status glyph, drawn beside the name.
 *
 *  Colour is never the only channel. A topology diagram is printed into method
 *  statements and photographed into tickets more than most screens in this
 *  product, and the status rail is 4px wide - the first thing a greyscale
 *  printer loses.
 */
function StatusGlyph({ status, severity }: { status: string; severity: string }) {
  const bad = status === 'OFFLINE' || severity === 'CRITICAL' || severity === 'MAJOR';
  const warn = severity === 'MINOR' || severity === 'WARNING';
  const unknown = status === 'UNKNOWN';
  const fill = statusFill(status, severity);
  if (unknown) {
    // A hollow ring: nothing has been heard, which is not the same as healthy.
    return <circle cx={0} cy={0} r={3} fill="none" stroke={fill} strokeWidth={1.4} />;
  }
  if (bad) {
    return <path d="M0,-4 L4,3 L-4,3 Z" fill={fill} />;      // triangle
  }
  if (warn) {
    return <path d="M0,-4 L4,0 L0,4 L-4,0 Z" fill={fill} />; // diamond
  }
  return <circle cx={0} cy={0} r={3} fill={fill} />;          // disc
}

/** How a redundancy side is drawn.
 *
 *  Position is the primary channel once the one-line layout lands (A left, B
 *  right); until then the pattern carries it, and hue is the last channel
 *  rather than the only one. The values come from the palette at render via
 *  `useChartColors` - this used to be `#3b82f6` and `#a855f7` written straight
 *  into `index.css`, a pair a deuteranope reads as one colour, on a
 *  distinction that decides whether someone pulls a live feed.
 *
 *  A stays solid because it is the common case and the cleanest stroke; B
 *  takes the dash-dot from the chart ramp. A PLAIN DASH IS NOT USED here: the
 *  `down` state already owns `4 3`, and a B-side feeder that is down must not
 *  look like a B-side feeder that is fine.
 */
function sideStyle(side: string | null, colors: ReturnType<typeof useChartColors>) {
  if (side === 'A') return { stroke: colors.series[0], dash: undefined };
  if (side === 'B') return { stroke: colors.series[1], dash: '10 4 2 4' };
  return { stroke: undefined, dash: undefined };
}

export function Diagram({ placement, edges, layer, onSelect, selected }: {
  placement: Placement;
  edges: CollapsedEdge[];
  layer: string;
  onSelect: (node: TopologyNode) => void;
  selected: string | null;
}) {
  const colors = useChartColors();

  const byId = new Map<string, Placed>();
  for (const p of placement.placed) byId.set(p.node.id, p);

  const sides = new Set<string>();
  for (const e of edges) for (const s of e.sides) sides.add(s);
  const showLegend = sides.size > 1;

  return (
    <>
      <div className="topo-wrap">
        <svg
          className="topo"
          viewBox={`${-placement.width / 2 - 20} -20 ${placement.width + 40} ${placement.height + 40}`}
          role="img"
          aria-label={`${layer} connectivity, ${placement.placed.length} devices`}
        >
          {edges.map((e) => (
            <Edge key={`${e.source}>${e.target}`} e={e} byId={byId} colors={colors} />
          ))}
          {placement.placed.map((p) => (
            <g key={p.node.id}
               className={`topo-node${selected === p.node.id ? ' is-selected' : ''}`}
               transform={`translate(${p.x},${p.y})`}
               role="button" tabIndex={0}
               aria-label={nodeLabel(p.node)}
               onClick={() => onSelect(p.node)}
               onKeyDown={(ev) => {
                 if (ev.key === 'Enter' || ev.key === ' ') {
                   ev.preventDefault();
                   onSelect(p.node);
                 }
               }}>
              <title>{nodeLabel(p.node)}</title>
              <rect width={NODE_W} height={NODE_H} rx={4}
                    className={p.node.depth > 0 ? 'topo-box outside' : 'topo-box'} />
              <rect width={4} height={NODE_H} rx={2}
                    fill={statusFill(p.node.status, p.node.max_severity)} />
              <g transform={`translate(${NODE_W - 11},${NODE_H / 2})`}>
                <StatusGlyph status={p.node.status} severity={p.node.max_severity} />
              </g>
              <text x={10} y={12} className="topo-name">{p.node.name}</text>
              <text x={10} y={23} className="topo-type">{p.node.device_type}</text>
            </g>
          ))}
        </svg>
      </div>

      {showLegend && (
        <div className="topo-legend">
          {[...sides].sort().map((s) => {
            const st = sideStyle(s, colors);
            return (
              <span key={s}>
                <svg width="18" height="9" aria-hidden>
                  <line x1="0" y1="4.5" x2="18" y2="4.5" strokeWidth="2"
                        stroke={st.stroke ?? 'var(--border-strong)'}
                        strokeDasharray={st.dash} />
                </svg>
                Side {s}
              </span>
            );
          })}
          <span>
            <svg width="18" height="9" aria-hidden>
              <line x1="0" y1="4.5" x2="18" y2="4.5" strokeWidth="2"
                    stroke="var(--critical)" strokeDasharray="4 3" />
            </svg>
            Down
          </span>
        </div>
      )}
    </>
  );
}

/** Reads as a sentence, because a screen reader announces it as one. */
function nodeLabel(n: TopologyNode): string {
  const parts = [n.name, n.device_type.replace(/_/g, ' '), n.status.toLowerCase()];
  if (n.max_severity && n.max_severity !== 'CLEAR') {
    parts.push(`${n.max_severity.toLowerCase()} alarm`);
  }
  if (n.location.rack_name) parts.push(`rack ${n.location.rack_name}`);
  else if (n.location.room_name) parts.push(n.location.room_name);
  if (n.depth > 0) parts.push(`${n.depth} hop${n.depth > 1 ? 's' : ''} outside the scope`);
  return parts.join(' · ');
}

function Edge({ e, byId, colors }: {
  e: CollapsedEdge;
  byId: Map<string, Placed>;
  colors: ReturnType<typeof useChartColors>;
}) {
  const a = byId.get(e.source);
  const b = byId.get(e.target);
  if (!a || !b) return null;

  const x1 = a.x + NODE_W / 2;
  const y1 = a.y + NODE_H;
  const x2 = b.x + NODE_W / 2;
  const y2 = b.y;
  // A vertical-tangent cubic, so links leave the bottom of a node and arrive at
  // the top of the next rather than cutting diagonally across the diagram.
  const mid = (y1 + y2) / 2;
  const d = `M${x1},${y1} C${x1},${mid} ${x2},${mid} ${x2},${y2}`;

  const down = e.downCount === e.count;
  const side = e.sides.length === 1 ? e.sides[0] : null;
  const st = sideStyle(side, colors);

  return (
    <path className={down ? 'topo-edge down' : 'topo-edge'} d={d}
          strokeWidth={e.count > 1 ? 1.8 : 1}
          stroke={st.stroke ?? 'var(--border-strong)'}
          strokeDasharray={st.dash}>
      <title>
        {e.count > 1 ? `${e.count} conductors` : '1 connection'}
        {side ? ` · side ${side}` : ''}
        {e.downCount ? ` · ${e.downCount} down` : ''}
      </title>
    </path>
  );
}
