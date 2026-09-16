import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useMemo, useRef, useState } from 'react';
import { api, type RoomSummary, type TopologyGraph, type TopologyNode }
  from '../../api/client';
import { Seg } from '../../components/estate';
import { MaxGlyph, MaxModal } from '../../components/MaxModal';
import { Diagram } from './Diagram';
import { collapseEdges, layout, structureKey } from './layout';
import './connectivity.css';

/** Connectivity: how the estate is wired, one layer at a time.
 *
 *  This page used to be the nav entry that led to the platform-health screen -
 *  CONNECTIVITY promised in the top row, collector lag delivered - while the
 *  graph itself sat at /topology with nothing linking to it. The nav now goes
 *  where it says, and /topology redirects here.
 *
 *  The layers are not five views of one graph. Power and cooling are directed
 *  distribution chains with a reading order; ethernet is an undirected fabric.
 *  This renders all five as layered diagrams for now - the one-line and loop
 *  renderings land with docs/24 phases 2 and 5.
 */

// The API accepts 'network' as an alias for the production enum; the operator's
// word is the one worth showing.
const LAYERS = [
  { key: 'power', label: 'POWER' },
  { key: 'cooling', label: 'COOLING' },
  { key: 'network', label: 'NETWORK' },
  { key: 'management', label: 'MANAGEMENT' },
  { key: 'fieldbus', label: 'FIELDBUS' },
] as const;

type LayerKey = typeof LAYERS[number]['key'];

/** Depth in words. "1" means nothing to someone who has not read the API spec,
 *  and the difference between 0 and 1 on the power layer is whether the
 *  switchgear feeding the room is in the picture at all. */
const DEPTHS = [
  { value: 0, label: 'Only what is in this room' },
  { value: 1, label: 'Also what feeds it' },
  { value: 2, label: 'Two hops out' },
];

export function Connectivity() {
  const [layer, setLayer] = useState<LayerKey>('power');
  const [roomId, setRoomId] = useState('');
  const [depth, setDepth] = useState(1);
  const [selected, setSelected] = useState<TopologyNode | null>(null);
  const [maxOpen, setMaxOpen] = useState(false);

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

  const title = `${LAYERS.find((l) => l.key === layer)?.label ?? layer} TOPOLOGY`;

  const diagram = graph.data ? (
    <Diagram placement={placement} edges={edges} layer={layer}
             selected={selected?.id ?? null}
             onSelect={(n) => setSelected(n)} />
  ) : null;

  return (
    <div className="stack">
      <h2>Connectivity</h2>
      <p className="muted">
        Drawn as layers, not a force graph: a power chain has a reading order —
        source at the top, load at the bottom — and positions computed from
        structure alone cannot drift when live state arrives.
      </p>

      <Seg value={layer} onChange={(v) => { setLayer(v); setSelected(null); }}
           label="Layer"
           options={LAYERS.map((l) => ({ key: l.key, label: l.label }))} />

      <div className="conn-filters">
        <select value={selectedRoom} aria-label="Room"
                onChange={(e) => { setRoomId(e.target.value); setSelected(null); }}>
          {rooms.data?.items.map((r) => (
            <option key={r.id} value={r.id}>
              {r.datacenter_code ? `${r.datacenter_code} · ` : ''}{r.name}
            </option>
          ))}
        </select>
        <select value={depth} aria-label="How far out to look"
                onChange={(e) => setDepth(Number(e.target.value))}>
          {DEPTHS.map((d) => (
            <option key={d.value} value={d.value}>{d.label}</option>
          ))}
        </select>
      </div>

      {graph.isError && (
        <p className="muted">
          No {layer} connections are recorded in this room. Plant serving a hall
          often sits elsewhere — widen the scope above to pull it in.
        </p>
      )}

      {graph.data && (
        <div className="conn-panel">
          <h3>
            {title}
            <span className="conn-total">
              {graph.data.node_count}<span className="unit"> devices</span>
            </span>
          </h3>
          <button type="button" className="asset-max" aria-label={`Maximize ${title}`}
                  onClick={() => setMaxOpen(true)}>
            <MaxGlyph />
          </button>

          {diagram}

          <p className="muted conn-caption">
            {graph.data.node_count} devices · {edges.length} connections
            {graph.data.edge_count !== edges.length && (
              <> (collapsed from {graph.data.edge_count} conductors)</>
            )}
            {graph.data.truncated && (
              <span className="warn"> · truncated — narrow the scope</span>
            )}
            {depth > 0 && ' · faded nodes were pulled in from outside the room'}
          </p>

          {selected && (
            <div className="conn-selected">
              <strong>{selected.name}</strong>
              <span className="muted">
                {selected.device_type.replace(/_/g, ' ')} ·{' '}
                {selected.status.toLowerCase()}
                {selected.location.rack_name
                  ? ` · rack ${selected.location.rack_name}`
                  : selected.location.room_name
                    ? ` · ${selected.location.room_name}` : ''}
              </span>
              <Link to={`/devices/${selected.id}`}>Open device →</Link>
            </div>
          )}
        </div>
      )}

      {maxOpen && (
        <MaxModal title={title} onClose={() => setMaxOpen(false)}>
          {diagram}
        </MaxModal>
      )}
    </div>
  );
}
