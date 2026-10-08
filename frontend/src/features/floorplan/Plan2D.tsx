import { Link, useNavigate } from 'react-router-dom';
import type { FloorEquipment, FloorPlan as Plan, FloorRack } from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { humanise } from '../../lib/format';
import { alarmColor, rackFill, type Overlay } from './colors';
import { fmtT } from './units';

/** The SVG plan: the room drawn to scale in metres. The fallback for a browser
 *  with no WebGL, and the reference the viewer's PLAN mode must agree with. */

function rackTitle(r: FloorRack): string {
  return [
    r.name,
    r.row_name ? `row ${r.row_name}` : null,
    `${r.device_count} devices`,
    r.load_kw != null ? `${r.load_kw.toFixed(1)} kW` : null,
    r.max_inlet_c != null ? `inlet ${fmtT(r.max_inlet_c)}` : 'no inlet reading',
    r.offline_count ? `${r.offline_count} offline` : null,
    r.facing ? `faces ${r.facing === 'N' ? 'north' : 'south'}` : null,
  ].filter(Boolean).join(' · ');
}

function equipTitle(e: FloorEquipment): string {
  return [
    e.name,
    humanise(e.device_type),
    e.w_m != null && e.d_m != null
      ? `${e.w_m} × ${e.d_m}${e.h_m != null ? ` × ${e.h_m}` : ''} m${e.basis === 'class' ? ' (class estimate)' : ''}`
      : e.mount ? `${e.mount}-mounted` : null,
    e.power_w != null ? `${(e.power_w / 1000).toFixed(1)} kW` : null,
    e.max_severity !== 'CLEAR' ? e.max_severity.toLowerCase() : null,
  ].filter(Boolean).join(' · ');
}

// Plan-view size of a footprint after its facing: a unit turned to face east or
// west shows its depth along x.
function planSize(e: FloorEquipment): [number, number] {
  const w = e.w_m ?? 0, d = e.d_m ?? 0;
  const quarter = Math.round((((e.facing_deg ?? 0) % 360) + 360) % 360 / 90) % 4;
  return quarter % 2 ? [d, w] : [w, d];
}

function frontEdge(e: FloorEquipment, x0: number, y0: number, w: number, h: number,
                   t: number): [number, number, number, number] | null {
  if (e.facing_deg == null) return null;
  const q = Math.round((((e.facing_deg % 360) + 360) % 360) / 90) % 4;
  if (q === 0) return [x0, y0, w, t];
  if (q === 1) return [x0 + w - t, y0, t, h];
  if (q === 2) return [x0, y0 + h - t, w, t];
  return [x0, y0, t, h];
}

export function Plan2D({ plan, overlay }: { plan: Plan; overlay: Overlay }) {
  const navigate = useNavigate();
  const { extent, racks, aisles, rack_w_m: rw, rack_d_m: rd } = plan;
  const equipment = plan.equipment ?? [];
  const open = (id: string) => navigate(`/devices/${id}`);
  const peakKw = Math.max(0, ...racks.map((r) => r.load_kw ?? 0));
  const pad = 0.3;

  return (
    <>
      <div className="floor-wrap">
        <svg className="floorplan"
             viewBox={`${-pad} ${-pad} ${extent.width_m + pad * 2} ${extent.depth_m + pad * 2}`}
             role="img" aria-label={`Floor plan of ${plan.room_name}`}>
          <rect x={0} y={0} width={extent.width_m} height={extent.depth_m}
                className={extent.derived ? 'floor-outline derived' : 'floor-outline'} />
          {aisles.map((a) => (
            <g key={`${a.y_start}-${a.y_end}`}>
              <rect x={0} y={a.y_start} width={extent.width_m} height={a.y_end - a.y_start}
                    className={`aisle aisle-${a.kind}`} />
              <text x={0.15} y={(a.y_start + a.y_end) / 2} className="aisle-label">
                {a.kind === 'unknown' ? 'aisle' : `${a.kind} aisle`}{a.label ? ` ${a.label}` : ''}
                {a.contained ? ' · contained' : ''}
              </text>
            </g>
          ))}
          {equipment.map((e) => {
            const x = e.x as number, y = e.y as number;
            const hit = {
              role: 'button', tabIndex: 0, 'aria-label': e.name, className: 'floor-equip-hit',
              onClick: () => open(e.id),
              onKeyDown: (ev: React.KeyboardEvent) => { if (ev.key === 'Enter' || ev.key === ' ') open(e.id); },
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
            const fs = Math.min(0.4, Math.max(0.12, Math.min(w, h) * 0.22));
            return (
              <g key={e.id} {...hit}>
                <title>{equipTitle(e)}</title>
                <rect x={x0} y={y0} width={w} height={h} className="floor-equip" style={fill ? { fill } : undefined} />
                {edge && <rect x={edge[0]} y={edge[1]} width={edge[2]} height={edge[3]} className="floor-front" />}
                {w > fs * 2.5 && h > fs * 1.4 && (
                  <text x={x} y={y} className="equip-tag" style={{ fontSize: `${fs}px` }}>{e.name.split('-')[0]}</text>
                )}
              </g>
            );
          })}
          {racks.map((r) => (
            <g key={r.id} role="button" tabIndex={0} aria-label={r.name} className="floor-rack-hit"
               onClick={() => navigate(`/racks/${r.id}?from=floorplan`)}
               onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') navigate(`/racks/${r.id}?from=floorplan`); }}>
              <title>{rackTitle(r)}</title>
              <rect x={r.x - (r.w_m ?? rw) / 2} y={r.y - (r.d_m ?? rd) / 2}
                    width={r.w_m ?? rw} height={r.d_m ?? rd}
                    fill={rackFill(r, overlay, peakKw)} className="floor-rack" />
              {r.facing && (
                <rect x={r.x - (r.w_m ?? rw) / 2}
                      y={r.facing === 'N' ? r.y - (r.d_m ?? rd) / 2 : r.y + (r.d_m ?? rd) / 2 - 0.08}
                      width={r.w_m ?? rw} height={0.08} className="floor-front" />
              )}
              <text x={r.x} y={r.y} className="rack-tag">{r.name}</text>
            </g>
          ))}
        </svg>
      </div>
      <p className="muted">
        {racks.length} racks{equipment.length > 0 && ` · ${equipment.length} other items placed`}
        {' · '}{extent.width_m} × {extent.depth_m} m
        {extent.derived && ' (outline derived from equipment positions)'}
      </p>
      {plan.unpositioned_equipment.length > 0 && (
        <section>
          <h3>Not placed</h3>
          <ul className="zero-u">
            {plan.unpositioned_equipment.map((e) => (
              <li key={e.id}><Link to={`/devices/${e.id}`}>{e.name}</Link>
                <span className="muted"> · {humanise(e.device_type)} · </span><StatusChip status={e.status} /></li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
