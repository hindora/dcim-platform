import { useQuery, useQueryClient } from '@tanstack/react-query';
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  api, type FloorPlan as Plan, type RoomKpi, type RoomSummary, type ThermalField, type ThermalRoom,
  type ThermalUnit, type TwinHistoryRange, type TwinRoomScene,
} from '../../api/client';
import { Seg } from '../../components/estate';
import type { Overlay } from './colors';
import {
  coolingLegendFor, DEFAULT_VIS, legendFor, rackTiers, ventTiles, TEMP_BANDS,
  type CoolingLayer, type HeatPlane, type RackLayer, type Sel, type ViewMode, type Visibility,
} from './layers';
import { DevicePanel, FiltersPanel, Legend, PathsPanel, RackPanel, RoomPanel, UnitPanel } from './panels';
import { fromImpact, fromTraces, type PathKind } from './paths';
import { Plan2D } from './Plan2D';
import { paneCaption, snap, sourceLabel, STEP_OPTIONS, Timeline, type CompareMode } from './Timeline';
import { numT, setTempUnit, unitLabel, useTempUnit, type TempUnit } from './units';
import { useFrames, useTopics } from '../../ws/useSocket';
import type { HoverTip } from './Scene';
import './viewer.css';

/**
 * Floor map: one room, drawn from its stored geometry and coloured by live
 * readings. 3D orbit, a top-down PLAN of the same scene, or a first-person walk
 * (FPV). Room, view and layers live in the URL so a view can be shared and
 * survives the back button. The WebGL stage is lazy; a browser without WebGL
 * gets the SVG plan.
 */

// three.js and friends load only when a room is opened.
const Stage = lazy(() => import('./Stage'));

const MODES: { key: ViewMode; label: string }[] = [
  { key: '3d', label: '3D' }, { key: 'plan', label: 'PLAN' }, { key: 'fpv', label: 'FPV' },
];
const RACK_KEYS = new Set<string>(['temperature', 'inlet_outlet', 'exhaust', 'variance', 'rh', 'power', 'utilisation', 'committed',
                                   'space', 'compliance', 'alarm', 'none']);
const COOLING_KEYS = new Set<string>(['air', 'supply', 'return', 'utilisation', 'none']);
const HEAT_KEYS = new Set<string>(['bottom', 'mid', 'top']);

function hasWebGL(): boolean {
  try {
    const c = document.createElement('canvas');
    return Boolean(c.getContext('webgl2') || c.getContext('webgl'));
  } catch {
    return false;
  }
}

/** The SVG fallback knows the older overlay vocabulary. */
function overlayFor(layer: RackLayer): Overlay {
  if (layer === 'temperature' || layer === 'compliance') return 'thermal';
  if (layer === 'power' || layer === 'utilisation' || layer === 'committed') return 'power';
  if (layer === 'space') return 'space';
  return 'alarm';
}

export function FloorPlanView() {
  const [params, setParams] = useSearchParams();
  const webgl = useMemo(hasWebGL, []);

  const rooms = useQuery<{ items: RoomSummary[] }>({ queryKey: ['rooms'], queryFn: () => api.rooms() });
  const requested = params.get('room') || '';
  const known = rooms.data?.items.some((r) => r.id === requested) ?? false;
  const roomId = (known ? requested : '') || rooms.data?.items[0]?.id || '';
  const room = rooms.data?.items.find((r) => r.id === roomId);

  const rawView = params.get('view');
  const mode: ViewMode = rawView === 'plan' || rawView === '2d' ? 'plan' : rawView === 'fpv' ? 'fpv' : '3d';
  const rackLayer = (RACK_KEYS.has(params.get('layer') ?? '') ? params.get('layer') : 'temperature') as RackLayer;
  const coolingLayer = (COOLING_KEYS.has(params.get('cooling') ?? '') ? params.get('cooling') : 'air') as CoolingLayer;
  const heat = (HEAT_KEYS.has(params.get('heat') ?? '') ? params.get('heat') : 'off') as HeatPlane;
  const setParam = (k: string, v: string) => { params.set(k, v); setParams(params, { replace: true }); };

  // --- time (docs/27 Phase 4) ----------------------------------------------
  // `t` in the URL is the moment shown; absent = live. `cmp` is the compare
  // pane's offset in seconds before the main moment (0 = the live room).
  const tRaw = params.get('t');
  const t = useMemo(() => {
    if (!tRaw) return null;
    const d = new Date(tRaw);
    return Number.isNaN(d.getTime()) ? null : snap(d);
  }, [tRaw]);
  const tIso = t ? t.toISOString() : null;
  // `cmp` is an offset in seconds before the main moment (0 = the live room),
  // or 'pick' with the compare row's own moment in `cmp_t`.
  const cmpRaw = params.get('cmp');
  const cmp: CompareMode = cmpRaw === 'pick' ? 'pick'
    : cmpRaw != null && cmpRaw !== '' && !Number.isNaN(Number(cmpRaw)) ? Number(cmpRaw) : null;
  const cmpTRaw = params.get('cmp_t');
  const pickedT = useMemo(() => {
    const d = cmpTRaw ? new Date(cmpTRaw) : null;
    return d && !Number.isNaN(d.getTime()) ? snap(d) : null;
  }, [cmpTRaw]);
  const cmpT = cmp === 'pick' ? (pickedT ?? snap(new Date(Date.now() - 3600_000)))
    : cmp == null || cmp === 0 ? null : new Date((t ?? snap(new Date())).getTime() - cmp * 1000);
  const cmpIso = cmpT ? cmpT.toISOString() : null;
  // Minutes per frame for play and the step buttons.
  const stepRaw = Number(params.get('step'));
  const step = STEP_OPTIONS.some((o) => o.value === stepRaw) ? stepRaw : 5;
  const [playing, setPlaying] = useState(false);
  const tUnit = useTempUnit();
  const setTime = (d: Date | null) => {
    if (d) params.set('t', snap(d).toISOString()); else params.delete('t');
    setParams(params, { replace: true });
  };
  const setCmp = (v: CompareMode) => {
    if (v == null) params.delete('cmp'); else params.set('cmp', String(v));
    if (v === 'pick' && !params.get('cmp_t')) {
      // Start the compare row an hour before whatever the main view shows.
      params.set('cmp_t', snap(new Date((t ?? new Date()).getTime() - 3600_000)).toISOString());
    }
    if (v !== 'pick') params.delete('cmp_t');
    setParams(params, { replace: true });
  };
  const setCmpTime = (d: Date) => { params.set('cmp_t', snap(d).toISOString()); setParams(params, { replace: true }); };
  const setStep = (m: number) => {
    if (m === 5) params.delete('step'); else params.set('step', String(m));
    setParams(params, { replace: true });
  };
  const live = t == null;

  const [panel, setPanel] = useState<'info' | 'filters' | null>('info');
  const [sel, setSel] = useState<Sel>(null);
  const [tip, setTip] = useState<HoverTip | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const [vis, setVis] = useState<Visibility>(DEFAULT_VIS);
  const [resetTick, setResetTick] = useState(0);
  const [themeTick, setThemeTick] = useState(0);
  const [pathKind, setPathKind] = useState<PathKind | null>(null);
  const qc = useQueryClient();

  useEffect(() => {
    const mo = new MutationObserver(() => setThemeTick((t) => t + 1));
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });
    return () => mo.disconnect();
  }, []);
  useEffect(() => { setSel(null); setFocus(null); setPathKind(null); }, [roomId]);
  useEffect(() => { setPathKind(null); }, [sel?.kind, sel?.id]);

  // Readings for this room arrive as one `room:{id}` frame per ingest batch;
  // the scene is re-read at most every 5 s on them, with the 60 s poll as the
  // backstop for a socket that has gone quiet.
  useTopics(roomId && live ? [`room:${roomId}`] : []);
  const lastPush = useRef(0);
  useFrames(['room_update'], (f) => {
    if (f.room_id !== roomId || !live) return;
    const now = Date.now();
    if (now - lastPush.current < 5_000) return;
    lastPush.current = now;
    qc.invalidateQueries({ queryKey: ['twin-scene', roomId] });
    qc.invalidateQueries({ queryKey: ['twin-field', roomId] });
  });
  // Labels are what FPV is for; in the other views they are clutter until asked for.
  useEffect(() => { setVis((v) => ({ ...v, labels: mode === 'fpv' })); }, [mode]);

  // Live: the scene, re-read on the room topic and every minute. Replay: one
  // frame per moment, which never changes once read.
  const sceneFor = (iso: string | null) => (iso ? api.roomFrame(roomId, iso) : api.roomScene(roomId));
  const scene = useQuery<TwinRoomScene>({
    queryKey: ['twin-scene', roomId, tIso ?? 'live'], queryFn: () => sceneFor(tIso),
    enabled: Boolean(roomId) && webgl, refetchInterval: live ? 60_000 : false,
    staleTime: live ? 0 : 10 * 60_000, retry: false, placeholderData: (prev) => prev,
  });
  const cmpScene = useQuery<TwinRoomScene>({
    queryKey: ['twin-scene', roomId, cmpIso ?? 'live'], queryFn: () => sceneFor(cmpIso),
    enabled: Boolean(roomId) && webgl && cmp != null, refetchInterval: cmpIso ? false : 60_000,
    staleTime: cmpIso ? 10 * 60_000 : 0, retry: false, placeholderData: (prev) => prev,
  });
  // Playback: five minutes forward every 1.5 s, the next frame fetched ahead
  // so the picture never waits. Stops at now, on going live, or on a new room.
  useEffect(() => { setPlaying(false); }, [roomId, live]);
  useEffect(() => {
    if (!playing || !t) return;
    const next = new Date(t.getTime() + step * 60_000);
    if (next > new Date()) { setPlaying(false); return; }
    const iso = next.toISOString();
    qc.prefetchQuery({ queryKey: ['twin-scene', roomId, iso], queryFn: () => api.roomFrame(roomId, iso),
                       staleTime: 10 * 60_000 });
    const id = window.setTimeout(() => setTime(next), 1500);
    return () => window.clearTimeout(id);
  }, [playing, t, roomId, step]); // eslint-disable-line react-hooks/exhaustive-deps
  // How far back the room goes: bounds the date picker and greys the scrubber.
  const history = useQuery<TwinHistoryRange>({
    queryKey: ['twin-history', roomId], queryFn: () => api.roomHistory(roomId),
    enabled: Boolean(roomId) && webgl, staleTime: 10 * 60_000, retry: false,
  });
  const earliest = history.data?.earliest ? new Date(history.data.earliest) : null;
  // A shared link to a moment the room has no record of opens at its oldest one.
  useEffect(() => {
    if (!earliest) return;
    if (t && t < earliest) setTime(new Date(earliest.getTime() + 5 * 60_000 - 1));
    if (cmp === 'pick' && pickedT && pickedT < earliest) setCmpTime(new Date(earliest.getTime() + 5 * 60_000 - 1));
  }, [history.data?.earliest, tIso, cmpTRaw]); // eslint-disable-line react-hooks/exhaustive-deps
  // The interpolated air is a second, heavier payload, asked for only while a
  // heat-map plane is showing; it moves when the scene moves.
  const field = useQuery<ThermalField>({
    queryKey: ['twin-field', roomId, tIso ?? 'live'], queryFn: () => api.roomField(roomId, tIso),
    enabled: Boolean(roomId) && webgl && heat !== 'off', refetchInterval: live ? 60_000 : false,
    staleTime: live ? 0 : 10 * 60_000, retry: false, placeholderData: (prev) => prev,
  });
  const plan2d = useQuery<Plan>({
    queryKey: ['floorplan', roomId], queryFn: () => api.floorplan(roomId),
    enabled: Boolean(roomId) && !webgl, refetchInterval: 20_000, retry: false,
  });
  // The room KPI is a rolling-hour aggregate over every reading in the room -
  // seconds on a quiet database, minutes on a busy one - and only the room
  // panel's compliance and power rows read it. Fetched once while that panel
  // is up, never polled: the picture itself comes from the scene.
  const kpi = useQuery<RoomKpi>({
    queryKey: ['room-kpi', roomId], queryFn: () => api.roomKpi(roomId),
    enabled: Boolean(roomId) && panel === 'info' && !sel, staleTime: 10 * 60_000, retry: false,
  });
  const thermal = useQuery<ThermalRoom>({
    queryKey: ['thermal-room', roomId], queryFn: () => api.thermal(roomId),
    enabled: Boolean(roomId), refetchInterval: 60_000, staleTime: 30_000, retry: false,
  });


  const units = useMemo(() => {
    const m = new Map<string, ThermalUnit>();
    for (const u of thermal.data?.crah_units ?? []) m.set(u.device_id, u);
    return m;
  }, [thermal.data]);

  const data = scene.data;
  const plan = data?.plan;

  // --- paths ------------------------------------------------------------
  // A rack is traced through its rack PDUs - the cords that feed it - so a
  // rack's power path is the union of its strips'. Impact is a question about
  // one device, so it is offered for a device or a unit only.
  const anchors = useMemo(() => {
    if (!sel || !data) return [] as string[];
    if (sel.kind === 'rack') return data.devices.filter((d) => d.rack_id === sel.id && d.device_type === 'pdu').map((d) => d.id);
    return [sel.id];
  }, [sel, data]);
  const pathQ = useQuery({
    queryKey: ['twin-path', pathKind, anchors],
    queryFn: async () => pathKind === 'impact'
      ? fromImpact(await api.impact(anchors[0]))
      : fromTraces(pathKind as 'power' | 'cooling', await Promise.all(anchors.map((a) => api.trace(a, pathKind as string)))),
    enabled: Boolean(pathKind) && anchors.length > 0, staleTime: 60_000, retry: false,
  });
  const overlay = pathKind && pathQ.data ? pathQ.data : null;
  const inRoom = useMemo(() => {
    const ids = new Set<string>();
    for (const d of data?.devices ?? []) ids.add(d.id);
    for (const e of data?.plan.equipment ?? []) ids.add(e.id);
    return (id: string) => ids.has(id);
  }, [data]);
  const vents = useMemo(() => plan ? ventTiles(plan.racks, plan.aisles, plan.room_class === 'white_space').length : 0, [plan]);
  const legends = useMemo(() => {
    const a = legendFor(rackLayer);
    const b = coolingLegendFor(coolingLayer);
    const h = heat !== 'off' ? { title: 'Heat map', unit: '°C', bands: TEMP_BANDS, none: 'no sensor reaches' } : null;
    // One temperature legend serves every layer read on it.
    const out = [a];
    if (b && !out.some((x) => x && x.bands === b.bands)) out.push(b);
    if (h && !out.some((x) => x && x.bands === h.bands)) out.push(h);
    return out.filter((x): x is NonNullable<typeof x> => x != null);
  }, [rackLayer, coolingLayer, heat, tUnit]); // eslint-disable-line react-hooks/exhaustive-deps

  const updatedAgo = live && scene.dataUpdatedAt ? Math.max(0, Math.round((Date.now() - scene.dataUpdatedAt) / 1000)) : null;
  const frameInfo = data?.frame ?? null;

  // --- selection panel content ----------------------------------------
  let panelTitle = 'Room information';
  let panelBody: React.ReactNode = null;
  if (panel === 'filters') {
    panelTitle = 'View filters';
    panelBody = <FiltersPanel rackLayer={rackLayer} coolingLayer={coolingLayer} heat={heat} vis={vis}
                              onRack={(l) => setParam('layer', l)} onCooling={(l) => setParam('cooling', l)}
                              onHeat={(h) => setParam('heat', h)} onVis={setVis} />;
  } else if (sel && data) {
    if (sel.kind === 'rack') {
      const r = data.plan.racks.find((x) => x.id === sel.id);
      if (r) {
        panelTitle = `Rack ${r.name}`;
        panelBody = <RackPanel rack={r} devices={data.devices.filter((d) => d.rack_id === r.id)}
                               index={data.indices?.racks.find((x) => x.rack_id === r.id)} />;
      }
    } else if (sel.kind === 'device') {
      const d = data.devices.find((x) => x.id === sel.id);
      if (d) { panelTitle = d.name; panelBody = <DevicePanel device={d} rack={data.plan.racks.find((r) => r.id === d.rack_id)} />; }
    } else {
      const e = data.plan.equipment.find((x) => x.id === sel.id);
      if (e) { panelTitle = e.name; panelBody = <UnitPanel unit={e} thermal={units.get(e.id)} />; }
    }
  } else if (data) {
    panelBody = <RoomPanel plan={data.plan} devices={data.devices} kpi={kpi.data} thermal={thermal.data} vents={vents}
                           indices={data.indices} field={heat !== 'off' ? field.data : null} />;
  }
  const paths = sel && data && panel === 'info' ? (
    <PathsPanel kind={pathKind} overlay={overlay} loading={Boolean(pathKind) && pathQ.isLoading} error={pathQ.isError}
                canImpact={sel.kind !== 'rack'} inRoom={inRoom}
                onPick={(k) => setPathKind(k)} onClear={() => setPathKind(null)} />
  ) : null;

  const focused = focus && data ? data.plan.racks.find((r) => r.id === focus) : null;
  const focusTiers = focused && data ? rackTiers(data.devices.filter((d) => d.rack_id === focused.id), focused.u_height ?? 42) : null;

  if (rooms.isLoading) return <p className="muted viewer-fallback">Loading…</p>;

  const roomSelect = (
    <select value={roomId} onChange={(e) => { params.set('room', e.target.value); setParams(params, { replace: true }); }}
            aria-label="Room">
      {rooms.data?.items.map((r) => (
        <option key={r.id} value={r.id}>{r.datacenter_code ? `${r.datacenter_code} · ` : ''}{r.name}</option>
      ))}
    </select>
  );

  if (!webgl) {
    return (
      <div className="viewer-fallback stack">
        <h2>Floor map</h2>
        <div className="floor-controls">
          <label>Room {roomSelect}</label>
          <Seg label="Layer" value={overlayFor(rackLayer)}
               onChange={(o) => setParam('layer', o === 'thermal' ? 'temperature' : o === 'power' ? 'utilisation' : o)}
               options={[{ key: 'thermal', label: 'Inlet temp' }, { key: 'power', label: 'Power' },
                         { key: 'space', label: 'Space' }, { key: 'alarm', label: 'Alarms' }]} />
        </div>
        <p className="muted">This browser has no WebGL, so the room is drawn as a plan.</p>
        {plan2d.isError && <p className="muted">Nothing in this room is positioned, so it cannot be drawn.</p>}
        {plan2d.data && <Plan2D plan={plan2d.data} overlay={overlayFor(rackLayer)} />}
      </div>
    );
  }

  const stage = (d: TwinRoomScene, withField: boolean) => (
    <Suspense fallback={<p className="muted viewer-fallback">Loading the room…</p>}>
      <Stage data={d} units={units} rackLayer={rackLayer} coolingLayer={coolingLayer} vis={vis} mode={mode}
             sel={sel} overlay={overlay} indices={d.indices} field={withField && heat !== 'off' ? field.data : null} heat={heat}
             resetTick={resetTick} themeTick={themeTick}
             onSelect={(s) => { setSel(s); if (s) setPanel('info'); }} onTip={setTip} onFocus={setFocus}
             label={`${mode === 'plan' ? 'Plan' : mode === 'fpv' ? 'Walk-through' : '3D view'} of ${d.plan.room_name}`} />
    </Suspense>
  );

  return (
    <div className={`viewer has-time${cmp === 'pick' ? ' has-compare-row' : ''}`}>
      <div className={`viewer-stage${cmp != null ? ' is-split' : ''}`}>
        {cmp == null && data && stage(data, true)}
        {cmp != null && (
          <>
            <div className="vw-pane">
              {data && stage(data, true)}
              <div className="vw-pane-cap"><b>{paneCaption(t, frameInfo)}</b></div>
            </div>
            <div className="vw-pane">
              {cmpScene.data && stage(cmpScene.data, false)}
              {cmpScene.isLoading && <p className="muted viewer-fallback">Loading the earlier room…</p>}
              <div className="vw-pane-cap">{paneCaption(cmpT, cmpScene.data?.frame)}</div>
            </div>
          </>
        )}
        {scene.isError && <p className="muted viewer-fallback">{live
          ? 'Nothing in this room is positioned, so it cannot be drawn.'
          : 'No frame for this moment. Try a later time or go back to live.'}</p>}
        {scene.isLoading && <p className="muted viewer-fallback">Loading the room…</p>}
      </div>

      <div className="vw-float vw-topleft">
        <span className="muted">Room</span>{roomSelect}
      </div>
      <div className="vw-float vw-topcenter">
        <Seg label="View" value={mode} onChange={(m) => setParam('view', m)} options={MODES} />
        <button type="button" className="vw-icon" onClick={() => setResetTick((t) => t + 1)} title="Reset the camera">RESET</button>
        <Seg label="Temperature unit" value={tUnit} onChange={(u) => setTempUnit(u as TempUnit)}
             options={[{ key: 'C', label: '°C' }, { key: 'F', label: '°F' }]} />
      </div>
      <div className="vw-rail">
        <button type="button" className={`vw-icon${panel === 'info' ? ' is-on' : ''}`}
                onClick={() => setPanel(panel === 'info' ? null : 'info')}>INFO</button>
        <button type="button" className={`vw-icon${panel === 'filters' ? ' is-on' : ''}`}
                onClick={() => setPanel(panel === 'filters' ? null : 'filters')}>LAYERS</button>
      </div>

      {panel && (
        <aside className="vw-panel" aria-label={panelTitle}>
          <div className="vw-panel-head">
            <h3>{panelTitle}</h3>
            <button type="button" className="close" aria-label="Close"
                    onClick={() => { if (sel && panel === 'info') setSel(null); else setPanel(null); }}>×</button>
          </div>
          {panelBody}
          {paths && <div className="vw-panel-body vw-paths">{paths}</div>}
        </aside>
      )}

      {legends.length > 0 && (
        <div className="vw-legends">{legends.map((l) => <Legend key={l.title} spec={l} />)}</div>
      )}

      {mode === 'fpv' && (
        <>
          <div className="vw-reticle" aria-hidden />
          {focused && focusTiers && (
            <div className="vw-focus">
              <b>{focused.name}</b>
              {focusTiers[0] != null || focusTiers[2] != null
                ? `Inlet ${focusTiers.map((x) => numT(x)).join(' / ')} ${unitLabel()}`
                : 'no inlet reading'}
              {focused.load_kw != null && ` · ${focused.load_kw.toFixed(1)} kW`}
              {focused.free_u != null && ` · ${focused.free_u} U free`}
            </div>
          )}
          <div className="vw-hint">Click the view to look around · W A S D to walk · Shift to hurry · Esc to release</div>
        </>
      )}

      {tip && mode !== 'fpv' && (
        <span className="hover-tip vw-tip" role="tooltip"
              style={{ left: Math.min(tip.x + 14, window.innerWidth - 300), top: tip.y + 14 }}>{tip.text}</span>
      )}

      <Timeline t={t} playing={playing} step={step} cmp={cmp} cmpT={cmpT} earliest={earliest}
                frame={frameInfo} cmpFrame={cmpScene.data?.frame}
                onLive={() => setTime(null)} onTime={setTime} onPlay={setPlaying} onStep={setStep}
                onCmp={setCmp} onCmpTime={setCmpTime} />

      <div className="vw-status">
        <span><span className="dot" style={{ background: scene.isError ? 'var(--critical)' : !live ? 'var(--accent)'
          : updatedAgo != null && updatedAgo < 45 ? 'var(--ok)' : 'var(--warn)' }} />
          {!live ? `Replay · ${t.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}${frameInfo ? ` · ${sourceLabel(frameInfo)}` : ''}`
            : updatedAgo == null ? 'Loading' : `Live · updated ${updatedAgo}s ago`}</span>
        <span className="vw-jumps">
          <Link to="/world">World</Link>
          {room?.datacenter_id && <Link to={`/twin/sites/${room.datacenter_id}`}>Building</Link>}
          <span>{room ? `${room.datacenter_code ? `${room.datacenter_code} · ` : ''}${room.name}` : ''}</span>
        </span>
        {plan && <span>{plan.extent.width_m} × {plan.extent.depth_m} m · {plan.racks.length} racks · {data?.devices.length ?? 0} devices</span>}
        {data?.indices?.rci_hi != null && (
          <span title="Rack Cooling Index (Herrlin): share of the way the intakes sit toward the allowable limit">
            RCI {Math.round(data.indices.rci_hi)} %{data.indices.hot_spots ? ` · ${data.indices.hot_spots} hot spot${data.indices.hot_spots === 1 ? '' : 's'}` : ''}
          </span>
        )}
        <span className="spacer" />
        <span>{mode === 'fpv' ? 'Pointer lock to look · W A S D to walk'
          : mode === 'plan' ? 'Drag to pan · scroll to zoom' : 'Drag to orbit · right-drag to pan · scroll to zoom'}</span>
      </div>
    </div>
  );
}
