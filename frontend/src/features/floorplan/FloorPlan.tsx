import { useQuery } from '@tanstack/react-query';
import { humanise } from '../../lib/format';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useState } from 'react';
import {
  api, type FloorEquipment, type FloorPlan as Plan, type FloorRack, type RoomSummary,
} from '../../api/client';
import { StatusChip } from '../../components/StatusChip';

type Overlay = 'thermal' | 'power' | 'alarm';

const OVERLAYS: { key: Overlay; label: string }[] = [
  { key: 'thermal', label: 'Inlet temp' },
  { key: 'power', label: 'Power' },
  { key: 'alarm', label: 'Alarms' },
];

// ASHRAE A1: 18-27 C recommended intake, allowable to 32. The scale is fixed to
// that band rather than to the room's own spread, so a room sitting at a
// uniform 24 C looks uniformly fine instead of manufacturing a hot spot out of
// half a degree.
const TEMP_MIN = 18;
const TEMP_RECOMMENDED_MAX = 27;
const TEMP_MAX = 32;

function tempColor(c: number | null | undefined): string {
  if (c == null) return 'var(--bg-inset)';
  const t = Math.min(1, Math.max(0, (c - TEMP_MIN) / (TEMP_MAX - TEMP_MIN)));
  // Blue (cold) through amber to red. hsl hue 210 -> 0.
  return `hsl(${Math.round(210 - 210 * t)}, 70%, ${Math.round(55 - 15 * t)}%)`;
}

function powerColor(kw: number | null | undefined, peak: number): string {
  if (kw == null || peak <= 0) return 'var(--bg-inset)';
  const t = Math.min(1, kw / peak);
  return `hsl(265, 60%, ${Math.round(22 + 38 * t)}%)`;
}

function alarmColor(sev: string, offline: number): string {
  if (offline > 0) return 'var(--critical)';
  switch (sev) {
    case 'CRITICAL': return 'var(--critical)';
    case 'MAJOR': return 'var(--major)';
    case 'MINOR':
    case 'WARNING': return 'var(--warn)';
    default: return 'var(--ok)';
  }
}

function rackFill(r: FloorRack, overlay: Overlay, peakKw: number): string {
  if (overlay === 'thermal') return tempColor(r.max_inlet_c);
  if (overlay === 'power') return powerColor(r.load_kw, peakKw);
  return alarmColor(r.max_severity, r.offline_count);
}

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
  const [overlay, setOverlay] = useState<Overlay>('thermal');

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
    enabled: Boolean(selected),
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

      {plan.isError && (
        <p className="muted">Nothing in this room is positioned, so it cannot be drawn.</p>
      )}

      {plan.data && <Plan2D plan={plan.data} overlay={overlay} />}
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
