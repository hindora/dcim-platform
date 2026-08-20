import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useMemo, useRef, useState } from 'react';
import { api, type RoomSummary, type TopologyGraph } from '../../api/client';
import {
  NODE_H, NODE_W, collapseEdges, layout, structureKey,
  type CollapsedEdge, type Placed,
} from './layout';

// The API accepts 'network' as an alias for the production enum; the operator's
// word is the one worth showing.
const LAYERS = [
  { key: 'power', label: 'Power' },
  { key: 'cooling', label: 'Cooling' },
  { key: 'network', label: 'Network' },
  { key: 'management', label: 'Management' },
  { key: 'fieldbus', label: 'Fieldbus' },
];

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

export function TopologyView() {
  const navigate = useNavigate();
  const [layer, setLayer] = useState('power');
  const [roomId, setRoomId] = useState('');
  const [depth, setDepth] = useState(1);

  const rooms = useQuery<{ items: RoomSummary[] }>({
    queryKey: ['rooms'],
    queryFn: () => api.rooms(),
  });
  const selectedRoom = roomId || rooms.data?.items[0]?.id || '';
  const scope = selectedRoom ? `room:${selectedRoom}` : '';

  const graph = useQuery<TopologyGraph>({
    queryKey: ['topology', layer, scope, depth],
    queryFn: () => api.topology(layer, scope, depth),
    enabled: Boolean(scope),
    // Live state. The layout below is memoised on structure, so this refresh
    // repaints statuses without moving a single node.
    refetchInterval: 15_000,
    retry: false,
  });

  const edges = useMemo(
    () => (graph.data ? collapseEdges(graph.data.edges) : []),
    [graph.data]);

  const key = graph.data ? structureKey(graph.data.nodes, graph.data.edges) : '';

  // Positions are recomputed ONLY when the structure key changes. A poll that
  // returns the same graph with new statuses reuses the previous placement
  // object, so nothing on screen moves.
  const cache = useRef<{ key: string; value: ReturnType<typeof layout> } | null>(null);
  const placement = useMemo(() => {
    if (!graph.data) return { placed: [], width: 0, height: 0 };
    if (cache.current?.key === key) return cache.current.value;
    const value = layout(graph.data.nodes, edges);
    cache.current = { key, value };
    return value;
  }, [graph.data, edges, key]);

  const byId = useMemo(() => {
    const m = new Map<string, Placed>();
    for (const p of placement.placed) m.set(p.node.id, p);
    return m;
  }, [placement]);

  return (
    <div className="stack">
      <h2>Topology</h2>
      <p className="muted">
        Drawn as layers, not a force graph: a power chain has a reading order —
        source at the top, load at the bottom — and positions computed from
        structure alone cannot drift when live state arrives.
      </p>

      <div className="floor-controls">
        <div className="overlay-picker" role="group" aria-label="Layer">
          {LAYERS.map((l) => (
            <button key={l.key} type="button"
                    className={layer === l.key ? 'active' : undefined}
                    onClick={() => setLayer(l.key)}>
              {l.label}
            </button>
          ))}
        </div>
        <label>
          Room{' '}
          <select value={selectedRoom} onChange={(e) => setRoomId(e.target.value)}>
            {rooms.data?.items.map((r) => (
              <option key={r.id} value={r.id}>
                {r.datacenter_code ? `${r.datacenter_code} · ` : ''}{r.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Depth{' '}
          <select value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
            {[0, 1, 2].map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
      </div>

      {graph.isError && <p className="warn">This layer has nothing in that scope.</p>}

      {graph.data && (
        <>
          <p className="muted">
            {graph.data.node_count} nodes · {edges.length} connections
            {graph.data.edge_count !== edges.length && (
              <> (collapsed from {graph.data.edge_count} conductors)</>
            )}
            {graph.data.truncated && <span className="warn"> · truncated — narrow the scope</span>}
            {depth > 0 && ' · faded nodes were pulled in from outside the room'}
          </p>

          <div className="topo-wrap">
            <svg
              className="topo"
              viewBox={`${-placement.width / 2 - 20} -20 ${placement.width + 40} ${placement.height + 40}`}
              role="img"
              aria-label={`${layer} topology`}
            >
              {edges.map((e) => <Edge key={`${e.source}>${e.target}`} e={e} byId={byId} />)}
              {placement.placed.map((p) => (
                <g key={p.node.id} className="topo-node"
                   transform={`translate(${p.x},${p.y})`}
                   role="button" tabIndex={0}
                   onClick={() => navigate(`/devices/${p.node.id}`)}
                   onKeyDown={(ev) => {
                     if (ev.key === 'Enter') navigate(`/devices/${p.node.id}`);
                   }}>
                  <title>
                    {`${p.node.name} · ${p.node.device_type} · ${p.node.status}`}
                    {p.node.depth > 0 ? ` · ${p.node.depth} hop(s) outside the scope` : ''}
                  </title>
                  <rect width={NODE_W} height={NODE_H} rx={4}
                        className={p.node.depth > 0 ? 'topo-box outside' : 'topo-box'} />
                  <rect width={4} height={NODE_H} rx={2}
                        fill={statusFill(p.node.status, p.node.max_severity)} />
                  <text x={10} y={12} className="topo-name">{p.node.name}</text>
                  <text x={10} y={23} className="topo-type">{p.node.device_type}</text>
                </g>
              ))}
            </svg>
          </div>
        </>
      )}
    </div>
  );
}

function Edge({ e, byId }: { e: CollapsedEdge; byId: Map<string, Placed> }) {
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

  const side = e.sides.length === 1 ? e.sides[0] : null;
  const cls = [
    'topo-edge',
    e.downCount === e.count ? 'down' : '',
    side === 'A' ? 'side-a' : side === 'B' ? 'side-b' : '',
  ].filter(Boolean).join(' ');

  return (
    <path className={cls} d={d} strokeWidth={e.count > 1 ? 1.8 : 1}>
      <title>
        {e.count > 1 ? `${e.count} conductors` : '1 connection'}
        {side ? ` · side ${side}` : ''}
        {e.downCount ? ` · ${e.downCount} down` : ''}
      </title>
    </path>
  );
}
