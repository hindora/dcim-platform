import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, type Impact, type RoomSummary, type TopologyGraph, type TopologyNode }
  from '../../api/client';
import { downloadCsv, stampedName } from '../../lib/csv';
import { projectImpact, type ImpactView } from './impact';
import { Audit } from './Audit';
import { Canvas } from './Canvas';
import { Drawer } from './Drawer';
import { Independence } from './Independence';
import { Legend } from './Legend';
import { SidePanel } from './SidePanel';
import { TraceTable } from './TraceTable';
import { collapseEdges, layout, layoutRooms, structureKey } from './layout';
import './connectivity.css';

/** Connectivity: how the estate is wired, one layer at a time.
 *
 *  THE CANVAS IS THE PAGE. Everything else — the layer strip, the scope, the
 *  trace table, the audit, the selection — floats on top of it. A diagram
 *  boxed into a panel under a heading and three rows of filters spends most of
 *  the screen on furniture, and the furniture is not what anybody came for.
 *
 *  It follows that a table cannot REPLACE the diagram, which is what the view
 *  tabs used to do. The trace and the audit are answers ABOUT what is on the
 *  canvas, and reading one while the thing it describes is gone is the same
 *  mistake as a modal over a diagram. They open as a sheet along the bottom
 *  and the graph stays where it was.
 */

// The API accepts 'network' as an alias for the production enum; the operator's
// word is the one worth showing. The ink is the layer's own colour, so the
// strip and the lines on the canvas agree.
const LAYERS = [
  { key: 'power', label: 'POWER', ink: 'var(--layer-power)' },
  { key: 'cooling', label: 'COOLING', ink: 'var(--layer-cooling)' },
  { key: 'network', label: 'NETWORK', ink: 'var(--layer-prod)' },
  { key: 'management', label: 'MANAGEMENT', ink: 'var(--layer-mgmt)' },
  { key: 'fieldbus', label: 'FIELDBUS', ink: 'var(--layer-fieldbus)' },
] as const;

type LayerKey = typeof LAYERS[number]['key'];

/** Sheets, not tabs. `null` is the canvas on its own. */
type Sheet = 'trace' | 'audit' | null;

/** Layers drawn as a one-line: A down the left, B down the right, shared and
 *  dual-fed equipment down the middle. */
const ONE_LINE = new Set(['power']);

/** Cooling is the layer that is a CIRCUIT rather than a chain. */
const LOOP = new Set(['cooling']);

export function Connectivity() {
  const [layer, setLayer] = useState<LayerKey>('power');
  const [sheet, setSheet] = useState<Sheet>(null);
  const [railOpen, setRailOpen] = useState(true);
  /** Bumped only when a selection comes from OFF the canvas, so the viewport
   *  moves for the device list and not for a node somebody just clicked. */
  const [focusNonce, setFocusNonce] = useState(0);
  const [siteId, setSiteId] = useState('');
  /** Empty means the WHOLE SITE. A room is a narrowing of a site, not a
   *  required choice: the question "how is this hall wired" and the question
   *  "how does this site hang together" are both real, and the second one had
   *  no way to be asked. */
  const [roomId, setRoomId] = useState('');
  const [depth, setDepth] = useState(1);
  const [rollup, setRollup] = useState<'none' | 'rack'>('rack');
  const [selected, setSelected] = useState<TopologyNode | null>(null);
  const [simulating, setSimulating] = useState<string | null>(null);

  // Deep link. `?simulate=<device>` lands here from wherever the question was
  // actually asked - a maintenance window, most of the time - and the room is
  // resolved from the device rather than carried in the URL, because most
  // callers know a device id and not which hall it is in.
  const [params, setParams] = useSearchParams();
  const linked = params.get('simulate');

  const linkedDevice = useQuery({
    queryKey: ['device', linked],
    queryFn: () => api.device(linked!),
    enabled: Boolean(linked),
    retry: false,
  });

  useEffect(() => {
    if (!linked) return;
    const wanted = params.get('layer');
    if (wanted && LAYERS.some((l) => l.key === wanted)) setLayer(wanted as LayerKey);
    setSimulating(linked);
    const room = linkedDevice.data?.location.room_id;
    const site = linkedDevice.data?.location.datacenter_id;
    if (site) setSiteId(site);
    if (room) setRoomId(room);
    // Consumed: leaving it in the URL re-applies it every time the operator
    // changes room afterwards, which fights them.
    if (room || linkedDevice.isError) setParams({}, { replace: true });
  }, [linked, linkedDevice.data, linkedDevice.isError, params, setParams]);

  const rooms = useQuery<{ items: RoomSummary[] }>({
    queryKey: ['rooms'],
    queryFn: () => api.rooms(),
  });

  // The sites come out of the room list rather than from a call of their own -
  // every room already carries the datacenter it is in, and a second request
  // to learn the same two facts would be a second thing to go stale.
  const sites = useMemo(() => {
    const seen = new Map<string, { id: string; code: string }>();
    for (const r of rooms.data?.items ?? []) {
      if (r.datacenter_id && !seen.has(r.datacenter_id)) {
        seen.set(r.datacenter_id,
                 { id: r.datacenter_id, code: r.datacenter_code || 'Site' });
      }
    }
    return [...seen.values()].sort((a, b) => a.code.localeCompare(b.code));
  }, [rooms.data]);

  const selectedSite = siteId || sites[0]?.id || '';
  const siteRooms = useMemo(
    () => (rooms.data?.items ?? []).filter((r) => r.datacenter_id === selectedSite),
    [rooms.data, selectedSite]);

  // A room from another site is not a narrowing of this one, so changing site
  // drops it rather than asking for a scope that contradicts itself.
  const selectedRoom = siteRooms.some((r) => r.id === roomId) ? roomId : '';

  // Whole site when no room is picked. Both are anchors the topology endpoint
  // already understood; nothing had ever asked it for the larger one.
  const scope = selectedRoom ? `room:${selectedRoom}`
    : selectedSite ? `datacenter:${selectedSite}` : '';
  const room = rooms.data?.items.find((r) => r.id === selectedRoom);
  const site = sites.find((s) => s.id === selectedSite);
  const scopeName = room?.name ?? (site ? `${site.code}, every room` : 'this room');

  const graph = useQuery<TopologyGraph>({
    queryKey: ['topology', layer, scope, depth, rollup],
    queryFn: () => api.topology(layer, scope, depth, rollup),
    enabled: Boolean(scope),
    // Live state. The layout is memoised on structure, so this repaints
    // statuses without moving a single node.
    refetchInterval: 15_000,
    retry: false,
  });

  const edges = useMemo(
    () => (graph.data ? collapseEdges(graph.data.edges) : []),
    [graph.data]);

  /** More than one room on the canvas means the site view, which is laid out
   *  by room rather than by rank - see layoutRooms. Read off the DATA, not off
   *  the scope: a room graph at depth 1 pulls in the plant that feeds it, and
   *  two rooms is two rooms however it got that way. */
  const byRoom = useMemo(() => {
    const seen = new Set<string>();
    for (const n of graph.data?.nodes ?? []) seen.add(n.location.room_name ?? '');
    return seen.size > 1;
  }, [graph.data]);

  // The layer is part of the key, not just the structure: it decides which
  // layout is used, and two layers could in principle return the same ids.
  const key = graph.data
    ? `${layer}:${byRoom ? 'rooms' : 'rank'}:`
      + structureKey(graph.data.nodes, graph.data.edges) : '';

  const cache = useRef<{ key: string; value: ReturnType<typeof layout> } | null>(null);
  const placement = useMemo(() => {
    if (!graph.data) return { placed: [], width: 0, height: 0 };
    if (cache.current?.key === key) return cache.current.value;
    const value = byRoom
      ? layoutRooms(graph.data.nodes)
      : layout(graph.data.nodes, edges, {
          oneLine: ONE_LINE.has(layer), loop: LOOP.has(layer) });
    cache.current = { key, value };
    return value;
  }, [graph.data, edges, key, layer, byRoom]);

  /** Changing layer or scope invalidates the selection and the simulation: the
   *  same device may not be on the next layer at all, and a drawer describing
   *  a chain that is no longer drawn is worse than an empty one. */
  function reselect<T>(set: (v: T) => void) {
    return (v: T) => { set(v); setSelected(null); setSimulating(null); };
  }

  // Not polled. A simulation is a question asked at one moment about one
  // hypothetical, and an answer that changed under the operator mid-read would
  // be worse than a stale one.
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

  /** Select and bring into view. Used by the list and by an audit finding -
   *  anything whose click happens somewhere other than the canvas itself. */
  function reveal(node: TopologyNode) {
    setSelected(node);
    setFocusNonce((n) => n + 1);
  }

  const empty = Boolean(graph.data) && graph.data!.node_count === 0;
  const layerLabel = LAYERS.find((l) => l.key === layer)?.label.toLowerCase() ?? layer;

  return (
    <div className="conn-app">
      <SidePanel
        sites={sites} siteId={selectedSite} onSite={reselect(setSiteId)}
        rooms={siteRooms}
        roomId={selectedRoom} onRoom={reselect(setRoomId)}
        depth={depth} onDepth={setDepth}
        rollup={rollup} onRollup={reselect(setRollup)}
        nodes={graph.data?.nodes ?? []}
        selected={selected?.id ?? null}
        onSelect={reveal}
        open={railOpen} onToggle={() => setRailOpen((v) => !v)}
        loading={graph.isLoading} />

      <Canvas
        placement={placement} edges={edges} layer={layer} layoutKey={key}
        selected={selected?.id ?? null} onSelect={setSelected} impact={impact}
        focusNonce={focusNonce}
      >
        {/* ---- top centre: the layer, which is what the canvas IS --------- */}
        <div className="cn-float cn-layers" role="group" aria-label="Layer">
          {LAYERS.map((l) => (
            <button key={l.key} type="button"
                    className={layer === l.key ? 'is-on' : undefined}
                    style={layer === l.key
                      ? { background: l.ink, borderColor: l.ink } : undefined}
                    onClick={() => reselect(setLayer)(l.key)}>
              {l.label}
            </button>
          ))}
        </div>

        {/* ---- top right: the two answers about what is on the canvas ---- */}
        <div className="cn-topright">
          <div className="cn-float cn-sheets" role="group" aria-label="Views">
            <button type="button" className={sheet === 'trace' ? 'is-on' : undefined}
                    onClick={() => setSheet(sheet === 'trace' ? null : 'trace')}>
              TRACE
            </button>
            <button type="button" className={sheet === 'audit' ? 'is-on' : undefined}
                    onClick={() => setSheet(sheet === 'audit' ? null : 'audit')}>
              REDUNDANCY
            </button>
          </div>
        </div>

        {/* ---- the hypothetical, across the top ---------------------------- */}
        {simulating && (
          <SimulationBanner
            name={impactQ.data?.device.name ?? '…'}
            view={impact}
            loading={impactQ.isLoading}
            error={impactQ.isError}
            layerLabel={layerLabel}
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

        {/* ---- bottom left: what is on screen, and what is not ------------ */}
        {graph.data && !empty && (
          <div className="cn-float cn-caption">
            <span>
              <b>{graph.data.node_count}</b> boxes
              {graph.data.device_count !== graph.data.node_count
                && <> for {graph.data.device_count} devices</>}
              {' · '}
              <b>{edges.length}</b> lines
              {graph.data.conductor_count !== edges.length
                && <> for {graph.data.conductor_count} conductors</>}
            </span>
            {graph.data.truncated && (
              <span className="warn">truncated — narrow the scope</span>
            )}
            {graph.data.unconnected_count > 0 && (
              <span>{graph.data.unconnected_count} not on this layer, not drawn</span>
            )}
            <Legend sides={sides} showLoad={layer === 'power'}
                    simulating={Boolean(simulating)} />
          </div>
        )}

        {/* ---- states ------------------------------------------------------ */}
        {graph.isLoading && (
          <div className="cn-centre"><p className="muted">Reading the graph…</p></div>
        )}
        {graph.isError && (
          <div className="cn-centre">
            <p className="muted">
              No {layerLabel} connections are recorded in this room. Plant that
              serves a hall often sits elsewhere — widen the scope.
            </p>
          </div>
        )}
        {empty && (
          <div className="cn-centre">
            <p className="muted">
              Nothing in this room is on the {layerLabel} layer.
              {graph.data!.unconnected_count > 0 && (
                <> All {graph.data!.unconnected_count} of its devices are
                  recorded without one.</>
              )}
            </p>
          </div>
        )}

        {/* ---- right: the selection --------------------------------------- */}
        {selected && (
          <Drawer node={selected} layer={layer}
                  onFullTrace={() => setSheet('trace')}
                  onSimulate={setSimulating}
                  simulating={simulating === selected.id}
                  onClose={() => setSelected(null)} />
        )}

        {/* ---- bottom: the answers ABOUT the canvas, not instead of it ----- */}
        {sheet && (
          <section className="cn-sheet" aria-label={
            sheet === 'trace' ? 'Trace table' : 'Redundancy audit'}>
            <header>
              <h3>{sheet === 'trace' ? 'TRACE TABLE' : 'REDUNDANCY AUDIT'}</h3>
              <button type="button" className="asset-max" aria-label="Close"
                      onClick={() => setSheet(null)}>✕</button>
            </header>
            <div className="cn-sheet-body">
              {sheet === 'trace' && (
                selected && selected.rolled_up === 0 ? (
                  <TraceTable deviceId={selected.id} deviceName={selected.name}
                              layer={layer} />
                ) : (
                  <p className="muted">
                    {selected
                      ? `${selected.name} stands for ${selected.rolled_up} devices. `
                        + 'Ungroup, or pick one of them, to trace a chain.'
                      : 'Pick a device on the canvas to trace its chain to source.'}
                  </p>
                )
              )}
              {sheet === 'audit' && (
                <>
                  <Independence nodes={graph.data?.nodes ?? []} layer={layer} />
                  <Audit
                    scope={scope} layer={layer}
                    roomName={scopeName}
                    onSelectDevice={(id) => {
                      const node = graph.data?.nodes.find(
                        (n) => n.id === id || n.member_ids.includes(id));
                      if (node) { reveal(node); setSheet(null); }
                    }} />
                </>
              )}
            </div>
          </section>
        )}
      </Canvas>
    </div>
  );
}

/** The strip that says what is being pretended, and how bad it is.
 *
 *  Over the canvas rather than in the drawer, because the diagram is showing a
 *  HYPOTHETICAL and somebody glancing at the screen has to be able to tell
 *  that from the estate as it stands. A picture of a failure that looks like a
 *  picture of the present is the most dangerous thing this page could render.
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
    <div className="cn-float cn-sim" role="status">
      <span className="cn-sim-tag">SIMULATING</span>
      <span className="cn-sim-text">
        {error ? <>Could not work out what depends on <b>{name}</b>.</>
          : loading ? <>Working out what depends on <b>{name}</b>…</>
          : view?.empty || (view?.cutCount === 0 && view?.degradedCount === 0) ? (
            <>Nothing on the {layerLabel} layer depends on <b>{name}</b>.</>
          ) : (
            <>
              <b>{name}</b> removed — <b>{view!.cutCount}</b>{' '}
              {view!.effectPlural}, <b>{view!.degradedCount}</b> lose a
              redundancy side
            </>
          )}
      </span>
      {!loading && !error && <button type="button" onClick={onExport}>Export</button>}
      <button type="button" className="is-clear" onClick={onClear}>Clear</button>
    </div>
  );
}
