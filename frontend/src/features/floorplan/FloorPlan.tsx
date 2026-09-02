import { useQuery } from '@tanstack/react-query';
import { humanise } from '../../lib/format';
import { Link, useNavigate } from 'react-router-dom';
import { useState } from 'react';
import { api, type FloorPlan as Plan, type FloorRack, type RoomSummary } from '../../api/client';
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

export function FloorPlanView() {
  const [roomId, setRoomId] = useState<string>('');
  const [overlay, setOverlay] = useState<Overlay>('thermal');

  const rooms = useQuery<{ items: RoomSummary[] }>({
    queryKey: ['rooms'],
    queryFn: () => api.rooms(),
  });

  const selected = roomId || rooms.data?.items[0]?.id || '';

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
      <h2>Floor plan</h2>

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
          {/* Room outline. Dashed because the source carries no room
              dimensions - this is the extent of the equipment plus a margin,
              not a surveyed wall. */}
          <rect x={0} y={0} width={extent.width_m} height={extent.depth_m}
                className="floor-outline" />

          {aisles.map((a) => (
            <g key={`${a.y_start}-${a.y_end}`}>
              <rect x={0} y={a.y_start} width={extent.width_m}
                    height={a.y_end - a.y_start}
                    className={`aisle aisle-${a.kind}`} />
              <text x={0.15} y={(a.y_start + a.y_end) / 2} className="aisle-label">
                {a.kind === 'unknown' ? 'aisle' : `${a.kind} aisle`}
                {a.label ? ` ${a.label}` : ''}
              </text>
            </g>
          ))}

          {racks.map((r) => (
            <g key={r.id} role="button" tabIndex={0} aria-label={r.name}
               className="floor-rack-hit"
               onClick={() => navigate(`/racks/${r.id}`)}
               onKeyDown={(e) => {
                 if (e.key === 'Enter' || e.key === ' ') navigate(`/racks/${r.id}`);
               }}>
              <title>{rackTitle(r)}</title>
              <rect
                x={r.x - rw / 2} y={r.y - rd / 2} width={rw} height={rd}
                fill={rackFill(r, overlay, peakKw)}
                className="floor-rack"
              />
              {/* A tick on the side the rack's intake faces. Which way a rack
                  points decides which aisle its air comes from, so it belongs
                  on the drawing rather than in a tooltip. */}
              {r.facing && (
                <rect
                  x={r.x - rw / 2}
                  y={r.facing === 'N' ? r.y - rd / 2 : r.y + rd / 2 - 0.08}
                  width={rw} height={0.08} className="floor-front"
                />
              )}
              <text x={r.x} y={r.y} className="rack-tag">{r.name}</text>
            </g>
          ))}
        </svg>
      </div>

      <p className="muted">
        {racks.length} racks · outline {extent.width_m} × {extent.depth_m} m
        {extent.derived && ' (derived from equipment positions — the source carries no room dimensions)'}
        {' · rack footprint assumed 600 × 1200 mm'}
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
          <h3>Plant in this room</h3>
          <p className="muted">
            Listed, not drawn: the source has no room coordinate for
            floor-standing plant — only a position in its fleet-wide diagram,
            which is not metres. Placing a CRAH from that would put it outside
            the room.
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
