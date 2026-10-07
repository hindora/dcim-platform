import { useQuery } from '@tanstack/react-query';
import { humanise } from '../../lib/format';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { lazy, Suspense } from 'react';
import { Seg } from '../../components/estate';
import {
  alarmColor, OVERLAYS, POWER_CRIT, POWER_WARN, rackFill, TEMP_MAX, TEMP_MIN,
  TEMP_RECOMMENDED_MAX, type Overlay,
} from './colors';

// three.js and react-three-fiber load only when somebody opens the 3D view.
const FloorPlan3D = lazy(() => import('./FloorPlan3D'));

type View = '2d' | '3d';
import {
  api, type FloorEquipment, type FloorPlan as Plan, type FloorRack, type RoomSummary,
} from '../../api/client';
import { StatusChip } from '../../components/StatusChip';

function rackTitle(r: FloorRack): string {
  const bits = [
    r.name,
    r.row_name ? `row ${r.row_name}` : null,
    `${r.device_count} devices`,
    r.load_kw != null ? `${r.load_kw.toFixed(1)} kW` : null,
    r.max_inlet_c != null ? `inlet ${r.max_inlet_c.toFixed(1)} °C` : 'no inlet reading',
    r.offline_count ? `${r.offline_count} offline` : null,
    r.facing ? `faces ${r.facing === 'N' ? 'north' : 'south'}` : null,
  ];
  return bits.filter(Boolean).join(' · ');
}

function equipTitle(e: FloorEquipment): string {
  const bits = [
    e.name,
    humanise(e.device_type),
    e.w_m != null && e.d_m != null
      ? `${e.w_m} × ${e.d_m}${e.h_m != null ? ` × ${e.h_m}` : ''} m${e.basis === 'class' ? ' (class estimate)' : ''}`
      : e.mount ? `${e.mount}-mounted` : null,
    e.power_w != null ? `${(e.power_w / 1000).toFixed(1)} kW` : null,
    e.max_severity !== 'CLEAR' ? e.max_severity.toLowerCase() : null,
  ];
  return bits.filter(Boolean).join(' · ');
}

// Plan-view size of a footprint after its facing: a unit turned to face east or
// west shows its depth along x.
function planSize(e: FloorEquipment): [number, number] {
  const w = e.w_m ?? 0, d = e.d_m ?? 0;
  const quarter = Math.round((((e.facing_deg ?? 0) % 360) + 360) % 360 / 90) % 4;
  return quarter % 2 ? [d, w] : [w, d];
}

// The edge the front is on, as a thin rect: [x, y, width, height].
function frontEdge(e: FloorEquipment, x0: number, y0: number, w: number, h: number,
                   t: number): [number, number, number, number] | null {
  if (e.facing_deg == null) return null;
  const q = Math.round((((e.facing_deg % 360) + 360) % 360) / 90) % 4;
  if (q === 0) return [x0, y0, w, t];
  if (q === 1) return [x0 + w - t, y0, t, h];
  if (q === 2) return [x0, y0 + h - t, w, t];
  return [x0, y0, t, h];
}

export function FloorPlanView() {
  // The room lives in the URL, not in component state. `OPEN FLOOR PLAN` on the
  // room drawer links to /floorplan?room=<id>, and a page that kept the choice
  // in useState ignored it and silently drew whichever room happened to sort
  // first - the link appeared to work and showed the wrong hall. Keeping it in
  // the query string also makes the view linkable and survives a back button.
  const [params, setParams] = useSearchParams();
  // View and overlay live in the URL beside the room, so a 3D link to a hot
  // rack is shareable and the back button returns to the same picture.
  const view: View = params.get('view') === '3d' ? '3d' : '2d';
  const overlay: Overlay = (OVERLAYS.some((o) => o.key === params.get('overlay'))
    ? params.get('overlay') : 'thermal') as Overlay;
  const setParam = (k: string, v: string) => {
    params.set(k, v);
    setParams(params, { replace: true });
  };
  const setOverlay = (o: Overlay) => setParam('overlay', o);

  const rooms = useQuery<{ items: RoomSummary[] }>({
    queryKey: ['rooms'],
    queryFn: () => api.rooms(),
  });

  const requested = params.get('room') || '';
  // A stale or hand-typed id must not leave the picker showing a room the list
  // does not contain: fall back to the first room, the same as no parameter.
  const known = rooms.data?.items.some((r) => r.id === requested) ?? false;
  const selected = (known ? requested : '') || rooms.data?.items[0]?.id || '';

  const setRoomId = (id: string) => {
    // `replace` so paging through rooms does not build a back-button trail the
    // reader has to click through to leave the page.
    params.set('room', id);
    setParams(params, { replace: true });
  };

  const plan = useQuery<Plan>({
    queryKey: ['floorplan', selected],
    queryFn: () => api.floorplan(selected),
    enabled: Boolean(selected) && view === '2d',
    refetchInterval: 20_000,
    retry: false,
  });

  if (rooms.isLoading) return <p className="muted">Loading…</p>;

  return (
    <div className="stack">
      <h2>Floor map</h2>

      <div className="floor-controls">
        <label>
          Room{' '}
          <select value={selected} onChange={(e) => setRoomId(e.target.value)}>
            {rooms.data?.items.map((r) => (
              <option key={r.id} value={r.id}>
                {r.datacenter_code ? `${r.datacenter_code} · ` : ''}{r.name}
              </option>
            ))}
          </select>
        </label>
        <Seg label="View" value={view} onChange={(v) => setParam('view', v)}
             options={[{ key: '2d', label: '2D' }, { key: '3d', label: '3D' }]} />
        <div className="overlay-picker" role="group" aria-label="Overlay">
          {OVERLAYS.map((o) => (
            <button key={o.key} type="button"
                    className={overlay === o.key ? 'active' : undefined}
                    onClick={() => setOverlay(o.key)}>
              {o.label}
            </button>
          ))}
        </div>
      </div>

      {view === '2d' && plan.isError && (
        <p className="muted">Nothing in this room is positioned, so it cannot be drawn.</p>
      )}

      {view === '2d' && plan.data && <Plan2D plan={plan.data} overlay={overlay} />}

      {view === '3d' && selected && (
        <Suspense fallback={<p className="muted">Loading the 3D view…</p>}>
          <FloorPlan3D roomId={selected} overlay={overlay} />
        </Suspense>
      )}
    </div>
  );
}

function Plan2D({ plan, overlay }: { plan: Plan; overlay: Overlay }) {
  const navigate = useNavigate();
  const { extent, racks, aisles, rack_w_m: rw, rack_d_m: rd } = plan;
  const equipment = plan.equipment ?? [];
  const open = (id: string) => navigate(`/devices/${id}`);
  const peakKw = Math.max(0, ...racks.map((r) => r.load_kw ?? 0));
  const pad = 0.3;

  return (
    <>
      <div className="floor-wrap">
        <svg
          className="floorplan"
          viewBox={`${-pad} ${-pad} ${extent.width_m + pad * 2} ${extent.depth_m + pad * 2}`}
          role="img"
          aria-label={`Floor plan of ${plan.room_name}`}
        >
          {/* Room outline: the room's own dimensions when the import carried
              them. Dashed only when it had to be derived from what stands in
              the room - then it is not a surveyed wall. */}
          <rect x={0} y={0} width={extent.width_m} height={extent.depth_m}
                className={extent.derived ? 'floor-outline derived' : 'floor-outline'} />

          {aisles.map((a) => (
            <g key={`${a.y_start}-${a.y_end}`}>
              <rect x={0} y={a.y_start} width={extent.width_m}
                    height={a.y_end - a.y_start}
                    className={`aisle aisle-${a.kind}`} />
              <text x={0.15} y={(a.y_start + a.y_end) / 2} className="aisle-label">
                {a.kind === 'unknown' ? 'aisle' : `${a.kind} aisle`}
                {a.label ? ` ${a.label}` : ''}
                {a.contained ? ' · contained' : ''}
              </text>
            </g>
          ))}

          {/* Plant and instruments at their stored positions and true
              footprints. Coloured only by the alarm overlay: the inlet and
              power scales are rack measures, and painting a chiller on them
              would read as a rack reading. */}
          {equipment.map((e) => {
            const x = e.x as number, y = e.y as number;
            const hit = {
              role: 'button', tabIndex: 0, 'aria-label': e.name,
              className: 'floor-equip-hit',
              onClick: () => open(e.id),
              onKeyDown: (ev: React.KeyboardEvent) => {
                if (ev.key === 'Enter' || ev.key === ' ') open(e.id);
              },
            } as const;
            const fill = overlay === 'alarm' ? alarmColor(e.max_severity, 0) : undefined;
            if (e.w_m == null || e.d_m == null) {
              const s = 0.18;
              return (
                <g key={e.id} {...hit}>
                  <title>{equipTitle(e)}</title>
                  <path d={`M${x} ${y - s} L${x + s} ${y} L${x} ${y + s} L${x - s} ${y} Z`}
                        className="floor-point" style={fill ? { fill } : undefined} />
                </g>
              );
            }
            const [w, h] = planSize(e);
            const x0 = x - w / 2, y0 = y - h / 2;
            const edge = frontEdge(e, x0, y0, w, h, Math.min(0.1, Math.min(w, h) * 0.15));
            const tag = e.name.split('-')[0];
            const fs = Math.min(0.4, Math.max(0.12, Math.min(w, h) * 0.22));
            return (
              <g key={e.id} {...hit}>
                <title>{equipTitle(e)}</title>
                <rect x={x0} y={y0} width={w} height={h} className="floor-equip"
                      style={fill ? { fill } : undefined} />
                {edge && <rect x={edge[0]} y={edge[1]} width={edge[2]} height={edge[3]}
                               className="floor-front" />}
                {w > fs * 2.5 && h > fs * 1.4 && (
                  <text x={x} y={y} className="equip-tag" style={{ fontSize: `${fs}px` }}>
                    {tag}
                  </text>
                )}
              </g>
            );
          })}

          {racks.map((r) => (
            <g key={r.id} role="button" tabIndex={0} aria-label={r.name}
               className="floor-rack-hit"
               onClick={() => navigate(`/racks/${r.id}?from=floorplan`)}
               onKeyDown={(e) => {
                 if (e.key === 'Enter' || e.key === ' ') navigate(`/racks/${r.id}?from=floorplan`);
               }}>
              <title>{rackTitle(r)}</title>
              <rect
                x={r.x - (r.w_m ?? rw) / 2} y={r.y - (r.d_m ?? rd) / 2}
                width={r.w_m ?? rw} height={r.d_m ?? rd}
                fill={rackFill(r, overlay, peakKw)}
                className="floor-rack"
              />
              {/* A tick on the side the rack's intake faces. Which way a rack
                  points decides which aisle its air comes from, so it belongs
                  on the drawing rather than in a tooltip. */}
              {r.facing && (
                <rect
                  x={r.x - (r.w_m ?? rw) / 2}
                  y={r.facing === 'N' ? r.y - (r.d_m ?? rd) / 2 : r.y + (r.d_m ?? rd) / 2 - 0.08}
                  width={r.w_m ?? rw} height={0.08} className="floor-front"
                />
              )}
              <text x={r.x} y={r.y} className="rack-tag">{r.name}</text>
            </g>
          ))}
        </svg>
      </div>

      <p className="muted">
        {racks.length} racks
        {equipment.length > 0 && ` · ${equipment.length} other items placed`}
        {' · '}{extent.width_m} × {extent.depth_m} m
        {extent.derived && ' (outline derived from equipment positions — no room dimensions imported)'}
        {plan.level && ` · level ${plan.level}`}
        {plan.level_elevation_m != null && ` (+${plan.level_elevation_m} m)`}
        {racks.some((r) => r.w_m == null) && ' · rack footprint assumed 600 × 1200 mm'}
        {plan.aisle_source === 'derived' && aisles.length > 0 && ' · aisles inferred from rack facing'}
      </p>

      {overlay === 'thermal' && (
        <p className="muted">
          Scaled to ASHRAE A1: {TEMP_MIN} °C to {TEMP_MAX} °C allowable,
          recommended up to {TEMP_RECOMMENDED_MAX} °C. Racks with no inlet
          reading are left unfilled rather than shown as cold.
        </p>
      )}
      {overlay === 'power' && (
        <p className="muted">
          Load against the rack's rating (its smallest single feed): amber from{' '}
          {POWER_WARN * 100} %, red from {POWER_CRIT * 100} %. A rack with no
          rating is shaded against the busiest rack in the room instead.
        </p>
      )}
      {overlay === 'space' && (
        <p className="muted">Share of the rack's U in use: pale is empty, deep is full.</p>
      )}

      {plan.unpositioned_equipment.length > 0 && (
        <section>
          <h3>Not placed</h3>
          <p className="muted">
            In this room, but imported without a room coordinate, so listed
            rather than drawn — a guessed position could put a CRAH outside its
            own room.
          </p>
          <ul className="zero-u">
            {plan.unpositioned_equipment.map((e) => (
              <li key={e.id}>
                <Link to={`/devices/${e.id}`}>{e.name}</Link>
                <span className="muted"> · {humanise(e.device_type)} · </span>
                <StatusChip status={e.status} />
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
