import { useQuery } from '@tanstack/react-query';
import { useMemo, useRef, useState } from 'react';
import { api, type Impact, type RoomSummary, type TopologyGraph, type TopologyNode }
  from '../../api/client';
import { downloadCsv, stampedName } from '../../lib/csv';
import { projectImpact, type ImpactView } from './impact';
import { Seg } from '../../components/estate';
import { MaxGlyph, MaxModal } from '../../components/MaxModal';
import { Canvas } from './Canvas';
import { Legend } from './Legend';
import { Drawer } from './Drawer';
import { TraceTable } from './TraceTable';
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

const VIEWS = [
  { key: 'diagram', label: 'DIAGRAM' },
  { key: 'trace', label: 'TRACE TABLE' },
] as const;

type ViewKey = typeof VIEWS[number]['key'];

/** Layers drawn as a one-line: A down the left, B down the right, shared and
 *  dual-fed equipment down the middle. Both are directed distribution chains
 *  with a labelled second path; ethernet has neither, and columns there would
 *  be two empty gutters around every switch. */
const ONE_LINE = new Set(['power', 'cooling']);

/** Depth in words. "1" means nothing to someone who has not read the API spec,
 *  and the difference between 0 and 1 on the power layer is whether the
 *  switchgear feeding the room is in the picture at all. */
const DEPTHS = [
  { value: 0, label: 'Only what is in this room' },
  { value: 1, label: 'Also what feeds it' },
  { value: 2, label: 'Two hops out' },
];

const ROLLUPS = [
  { value: 'rack', label: 'Group rack equipment' },
  { value: 'none', label: 'Every device separately' },
] as const;

export function Connectivity() {
  const [layer, setLayer] = useState<LayerKey>('power');
  const [view, setView] = useState<ViewKey>('diagram');
  const [roomId, setRoomId] = useState('');
  const [depth, setDepth] = useState(1);
  const [rollup, setRollup] = useState<'none' | 'rack'>('rack');
  const [selected, setSelected] = useState<TopologyNode | null>(null);
  const [maxOpen, setMaxOpen] = useState(false);
  /** The device whose removal is being simulated, or null. Kept as an id
   *  rather than a node so that it survives a refetch replacing the objects. */
  const [simulating, setSimulating] = useState<string | null>(null);

  const rooms = useQuery<{ items: RoomSummary[] }>({
    queryKey: ['rooms'],
    queryFn: () => api.rooms(),
  });
  const selectedRoom = roomId || rooms.data?.items[0]?.id || '';
  const scope = selectedRoom ? `room:${selectedRoom}` : '';

  const graph = useQuery<TopologyGraph>({
    queryKey: ['topology', layer, scope, depth, rollup],
    queryFn: () => api.topology(layer, scope, depth, rollup),
    enabled: Boolean(scope),
    // Live state. The layout below is memoised on structure, so this refresh
    // repaints statuses without moving a single node.
    refetchInterval: 15_000,
    retry: false,
  });

  const edges = useMemo(
    () => (graph.data ? collapseEdges(graph.data.edges) : []),
    [graph.data]);

  // The layer is part of the key, not just the structure: it decides whether
  // the layout draws A and B as columns, and two layers could in principle
  // return the same node and edge ids.
  const key = graph.data
    ? `${layer}:${structureKey(graph.data.nodes, graph.data.edges)}` : '';

  // Positions are recomputed ONLY when the structure key changes. A poll that
  // returns the same graph with new statuses reuses the previous placement
  // object, so nothing on screen moves.
  const cache = useRef<{ key: string; value: ReturnType<typeof layout> } | null>(null);
  const placement = useMemo(() => {
    if (!graph.data) return { placed: [], width: 0, height: 0 };
    if (cache.current?.key === key) return cache.current.value;
    const value = layout(graph.data.nodes, edges, { oneLine: ONE_LINE.has(layer) });
    cache.current = { key, value };
    return value;
  }, [graph.data, edges, key, layer]);

  const title = `${LAYERS.find((l) => l.key === layer)?.label ?? layer} TOPOLOGY`;

  /** Selecting a layer or a scope invalidates the selection: the same device
   *  may not be on the next layer at all, and a drawer describing a chain that
   *  is no longer drawn is worse than an empty one. */
  function reselect<T>(set: (v: T) => void) {
    return (v: T) => { set(v); setSelected(null); setSimulating(null); };
  }

  // Not polled. A simulation is a question asked at one moment about one
  // hypothetical, and an answer that changed under the operator while they
  // read it would be worse than a stale one.
  const impactQ = useQuery<Impact>({
    queryKey: ['impact', simulating],
    queryFn: () => api.impact(simulating!),
    enabled: Boolean(simulating),
    retry: false,
  });

  const impact = useMemo(
    () => (impactQ.data ? projectImpact(impactQ.data, layer) : null),
    [impactQ.data, layer]);

  const sides = useMemo(() => {
    const s = new Set<string>();
    for (const e of edges) for (const x of e.sides) s.add(x);
    return [...s].sort();
  }, [edges]);

  const diagram = graph.data ? (
    <>
      {simulating && (
        <SimulationBanner
          name={impactQ.data?.device.name ?? '…'}
          view={impact}
          loading={impactQ.isLoading}
          error={impactQ.isError}
          layerLabel={LAYERS.find((l) => l.key === layer)?.label.toLowerCase() ?? layer}
          onExport={() => {
            const d = impactQ.data;
            if (!d) return;
            downloadCsv(
              stampedName(`impact-${d.device.name}`),
              ['Layer', 'Effect', 'Outcome', 'Device', 'Type', 'Rack', 'Room'],
              d.layers.flatMap((l) => [
                ...l.cut_off.map((n) => [l.layer, l.effect, 'cut off', n.name,
                                         n.device_type, n.rack_name, n.room_name]),
                ...l.degraded.map((n) => [l.layer, l.effect, 'degraded', n.name,
                                          n.device_type, n.rack_name, n.room_name]),
              ]),
            );
          }}
          onClear={() => setSimulating(null)} />
      )}
      <Canvas placement={placement} edges={edges} layer={layer} layoutKey={key}
              selected={selected?.id ?? null} onSelect={setSelected}
              impact={impact} />
      <Legend sides={sides} showLoad={layer === 'power'} simulating={Boolean(simulating)} />
    </>
  ) : null;

  return (
    <div className="stack">
      <h2>Connectivity</h2>
      <p className="muted">
        Drawn as layers, not a force graph: a power chain has a reading order —
        source at the top, load at the bottom — and positions computed from
        structure alone cannot drift when live state arrives.
      </p>

      {/* `.seg` is a block-level flex container, so on its own it stretches to
          the page width and hangs four empty cells' worth of border off the
          right. Every other use of it sits in a title row that already
          constrains it. */}
      <div className="conn-layerbar">
        <Seg value={layer} onChange={reselect(setLayer)} label="Layer"
             options={LAYERS.map((l) => ({ key: l.key, label: l.label }))} />
        <span className="spacer" />
        <Seg value={view} onChange={setView} label="View"
             options={VIEWS.map((v) => ({ key: v.key, label: v.label }))} />
      </div>

      <div className="conn-filters">
        <select value={selectedRoom} aria-label="Room"
                onChange={(e) => reselect(setRoomId)(e.target.value)}>
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
        <select value={rollup} aria-label="Grouping"
                onChange={(e) => reselect(setRollup)(e.target.value as 'none' | 'rack')}>
          {ROLLUPS.map((r) => (
            <option key={r.value} value={r.value}>{r.label}</option>
          ))}
        </select>
      </div>

      {graph.isError && (
        <p className="muted">
          No {layer} connections are recorded in this room. Plant serving a hall
          often sits elsewhere — widen the scope above to pull it in.
        </p>
      )}

      {view === 'trace' && (
        <div className="conn-panel">
          <h3>TRACE TABLE</h3>
          {selected && selected.rolled_up === 0 ? (
            <TraceTable deviceId={selected.id} deviceName={selected.name}
                        layer={layer} />
          ) : (
            <p className="muted">
              {selected
                ? `${selected.name} stands for ${selected.rolled_up} devices. `
                  + 'Group rack equipment off, or pick one of them, to trace a chain.'
                : 'Pick a device on the diagram to trace its chain to source.'}
            </p>
          )}
        </div>
      )}

      {view === 'diagram' && graph.data && (
        <div className="conn-body">
          <div className="conn-panel">
            <h3>
              {title}
              <span className="conn-total">
                {graph.data.device_count}<span className="unit"> devices</span>
              </span>
            </h3>
            <button type="button" className="asset-max" aria-label={`Maximize ${title}`}
                    onClick={() => setMaxOpen(true)}>
              <MaxGlyph />
            </button>

            {diagram}

            <p className="muted conn-caption">
              {graph.data.node_count} boxes
              {graph.data.device_count !== graph.data.node_count
                && <> for {graph.data.device_count} devices</>}
              {' · '}
              {edges.length} lines
              {graph.data.conductor_count !== edges.length
                && <> for {graph.data.conductor_count} conductors</>}
              {graph.data.truncated && (
                <span className="warn"> · truncated — narrow the scope</span>
              )}
              {depth > 0 && ' · faded nodes were pulled in from outside the room'}
              {graph.data.unconnected_count > 0 && (
                <>
                  {' · '}{graph.data.unconnected_count} device
                  {graph.data.unconnected_count === 1 ? '' : 's'} in this room
                  {' '}{graph.data.unconnected_count === 1 ? 'has' : 'have'} no
                  {' '}{layer} connection recorded and {graph.data.unconnected_count === 1
                    ? 'is' : 'are'} not drawn
                </>
              )}
            </p>
          </div>

          {selected && (
            <Drawer node={selected} layer={layer}
                    onFullTrace={() => setView('trace')}
                    onSimulate={setSimulating}
                    simulating={simulating === selected.id}
                    onClose={() => setSelected(null)} />
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

/** The strip that says what is being pretended, and how bad it is.
 *
 *  Above the canvas rather than inside the drawer, because the diagram is
 *  showing a HYPOTHETICAL and somebody glancing at the screen has to be able
 *  to tell that from the estate as it stands. A picture of a failure that
 *  looks like a picture of the present is the most dangerous thing this page
 *  could render.
 */
function SimulationBanner({ name, view, loading, error, layerLabel, onExport, onClear }: {
  name: string;
  view: ImpactView | null;
  loading: boolean;
  error: boolean;
  layerLabel: string;
  onExport: () => void;
  onClear: () => void;
}) {
  return (
    <div className="conn-sim" role="status">
      <span className="conn-sim-tag">SIMULATING</span>
      <span className="conn-sim-text">
        {error ? <>Could not work out what depends on <b>{name}</b>.</>
          : loading ? <>Working out what depends on <b>{name}</b>…</>
          : view?.empty || (view?.cutCount === 0 && view?.degradedCount === 0) ? (
            <>Nothing on the {layerLabel} layer depends on <b>{name}</b>.</>
          ) : (
            <>
              <b>{name}</b> removed — <b>{view!.cutCount}</b>{' '}
              {view!.effect}, <b>{view!.degradedCount}</b> lose a redundancy side
            </>
          )}
      </span>
      {!loading && !error && <button type="button" onClick={onExport}>Export list</button>}
      <button type="button" className="is-clear" onClick={onClear}>Clear</button>
    </div>
  );
}
