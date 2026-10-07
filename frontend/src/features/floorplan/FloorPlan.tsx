import { useQuery } from '@tanstack/react-query';
import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  api, type FloorPlan as Plan, type RoomKpi, type RoomSummary, type ThermalRoom, type ThermalUnit,
  type TwinRoomScene,
} from '../../api/client';
import { Seg } from '../../components/estate';
import type { Overlay } from './colors';
import {
  coolingLegendFor, DEFAULT_VIS, legendFor, rackTiers, ventTiles,
  type CoolingLayer, type RackLayer, type Sel, type ViewMode, type Visibility,
} from './layers';
import { DevicePanel, FiltersPanel, Legend, RackPanel, RoomPanel, UnitPanel } from './panels';
import { Plan2D } from './Plan2D';
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
const RACK_KEYS = new Set<string>(['temperature', 'rh', 'power', 'utilisation', 'space', 'compliance', 'alarm', 'none']);
const COOLING_KEYS = new Set<string>(['air', 'supply', 'return', 'utilisation', 'none']);

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
  if (layer === 'power' || layer === 'utilisation') return 'power';
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
  const setParam = (k: string, v: string) => { params.set(k, v); setParams(params, { replace: true }); };

  const [panel, setPanel] = useState<'info' | 'filters' | null>('info');
  const [sel, setSel] = useState<Sel>(null);
  const [tip, setTip] = useState<HoverTip | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const [vis, setVis] = useState<Visibility>(DEFAULT_VIS);
  const [resetTick, setResetTick] = useState(0);
  const [themeTick, setThemeTick] = useState(0);

  useEffect(() => {
    const mo = new MutationObserver(() => setThemeTick((t) => t + 1));
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });
    return () => mo.disconnect();
  }, []);
  useEffect(() => { setSel(null); setFocus(null); }, [roomId]);
  // Labels are what FPV is for; in the other views they are clutter until asked for.
  useEffect(() => { setVis((v) => ({ ...v, labels: mode === 'fpv' })); }, [mode]);

  const scene = useQuery<TwinRoomScene>({
    queryKey: ['twin-scene', roomId], queryFn: () => api.roomScene(roomId),
    enabled: Boolean(roomId) && webgl, refetchInterval: 15_000, retry: false,
  });
  const plan2d = useQuery<Plan>({
    queryKey: ['floorplan', roomId], queryFn: () => api.floorplan(roomId),
    enabled: Boolean(roomId) && !webgl, refetchInterval: 20_000, retry: false,
  });
  const kpi = useQuery<RoomKpi>({
    queryKey: ['room-kpi', roomId], queryFn: () => api.roomKpi(roomId),
    enabled: Boolean(roomId), refetchInterval: 30_000, retry: false,
  });
  const thermal = useQuery<ThermalRoom>({
    queryKey: ['thermal-room', roomId], queryFn: () => api.thermal(roomId),
    enabled: Boolean(roomId), refetchInterval: 30_000, retry: false,
  });
  const units = useMemo(() => {
    const m = new Map<string, ThermalUnit>();
    for (const u of thermal.data?.crah_units ?? []) m.set(u.device_id, u);
    return m;
  }, [thermal.data]);

  const data = scene.data;
  const plan = data?.plan;
  const vents = useMemo(() => plan ? ventTiles(plan.racks, plan.aisles, plan.room_class === 'white_space').length : 0, [plan]);
  const legends = useMemo(() => {
    const a = legendFor(rackLayer);
    const b = coolingLegendFor(coolingLayer);
    // One temperature legend serves both when both are read on it.
    return [a, b && (!a || b.bands !== a.bands) ? b : null].filter((x): x is NonNullable<typeof x> => x != null);
  }, [rackLayer, coolingLayer]);

  const updatedAgo = scene.dataUpdatedAt ? Math.max(0, Math.round((Date.now() - scene.dataUpdatedAt) / 1000)) : null;

  // --- selection panel content ----------------------------------------
  let panelTitle = 'Room information';
  let panelBody: React.ReactNode = null;
  if (panel === 'filters') {
    panelTitle = 'View filters';
    panelBody = <FiltersPanel rackLayer={rackLayer} coolingLayer={coolingLayer} vis={vis}
                              onRack={(l) => setParam('layer', l)} onCooling={(l) => setParam('cooling', l)} onVis={setVis} />;
  } else if (sel && data) {
    if (sel.kind === 'rack') {
      const r = data.plan.racks.find((x) => x.id === sel.id);
      if (r) { panelTitle = `Rack ${r.name}`; panelBody = <RackPanel rack={r} devices={data.devices.filter((d) => d.rack_id === r.id)} />; }
    } else if (sel.kind === 'device') {
      const d = data.devices.find((x) => x.id === sel.id);
      if (d) { panelTitle = d.name; panelBody = <DevicePanel device={d} rack={data.plan.racks.find((r) => r.id === d.rack_id)} />; }
    } else {
      const e = data.plan.equipment.find((x) => x.id === sel.id);
      if (e) { panelTitle = e.name; panelBody = <UnitPanel unit={e} thermal={units.get(e.id)} />; }
    }
  } else if (data) {
    panelBody = <RoomPanel plan={data.plan} devices={data.devices} kpi={kpi.data} thermal={thermal.data} vents={vents} />;
  }

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

  return (
    <div className="viewer">
      <div className="viewer-stage">
        {data && (
          <Suspense fallback={<p className="muted viewer-fallback">Loading the room…</p>}>
            <Stage data={data} units={units} rackLayer={rackLayer} coolingLayer={coolingLayer} vis={vis} mode={mode}
                   sel={sel} resetTick={resetTick} themeTick={themeTick}
                   onSelect={(s) => { setSel(s); if (s) setPanel('info'); }} onTip={setTip} onFocus={setFocus}
                   label={`${mode === 'plan' ? 'Plan' : mode === 'fpv' ? 'Walk-through' : '3D view'} of ${data.plan.room_name}`} />
          </Suspense>
        )}
        {scene.isError && <p className="muted viewer-fallback">Nothing in this room is positioned, so it cannot be drawn.</p>}
        {scene.isLoading && <p className="muted viewer-fallback">Loading the room…</p>}
      </div>

      <div className="vw-float vw-topleft">
        <span className="muted">Room</span>{roomSelect}
      </div>
      <div className="vw-float vw-topcenter">
        <Seg label="View" value={mode} onChange={(m) => setParam('view', m)} options={MODES} />
        <button type="button" className="vw-icon" onClick={() => setResetTick((t) => t + 1)} title="Reset the camera">RESET</button>
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
                ? `Inlet ${focusTiers.map((t) => (t == null ? '–' : t.toFixed(1))).join(' / ')} °C`
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

      <div className="vw-status">
        <span><span className="dot" style={{ background: scene.isError ? 'var(--critical)' : updatedAgo != null && updatedAgo < 45 ? 'var(--ok)' : 'var(--warn)' }} />
          {updatedAgo == null ? 'Loading' : `Live · updated ${updatedAgo}s ago`}</span>
        {room && <span>{room.datacenter_code ? `${room.datacenter_code} · ` : ''}{room.name}</span>}
        {plan && <span>{plan.extent.width_m} × {plan.extent.depth_m} m · {plan.racks.length} racks · {data?.devices.length ?? 0} devices</span>}
        <span className="spacer" />
        <span>Drag to orbit · right-drag to pan · scroll to zoom</span>
      </div>
    </div>
  );
}
