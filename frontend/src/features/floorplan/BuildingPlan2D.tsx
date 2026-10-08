import type { TwinRoom, TwinSiteScene } from '../../api/client';
import { alarmColor } from './colors';

/**
 * The building without WebGL: its levels stacked in a 30° oblique SVG, each
 * room in place and coloured by its worst open condition. The same picker the
 * site page used to be, now the fallback of the one viewer (docs/27 §8b).
 */

const COS = 0.866, SIN = 0.5;
// Vertical drawing units per metre of elevation. Exaggerated: at 1:1 a 5 m
// floor-to-floor is less than a 12 m hall's projected depth and the levels
// overlap; at this ratio each level clears the one below.
const RISE = 2.6;

function project(x: number, y: number, elev: number): [number, number] {
  return [(x - y) * COS, (x + y) * SIN - elev * RISE];
}

export function BuildingPlan2D({ scene, hover, onHover, onEnter }: {
  scene: TwinSiteScene; hover: string | null; onHover: (id: string | null) => void; onEnter: (id: string) => void;
}) {
  const placed = scene.rooms.filter((r) => r.origin_x_m != null && r.origin_y_m != null && r.width_m && r.depth_m);
  const levels = [...scene.levels].sort((a, b) => a.ordinal - b.ordinal);
  const elevOf = (r: TwinRoom) => r.level_elevation_m ?? levels.find((l) => l.name === r.level)?.elevation_m ?? 0;

  const pts: [number, number][] = [];
  for (const r of placed) {
    const e = elevOf(r);
    const x0 = r.origin_x_m as number, y0 = r.origin_y_m as number, w = r.width_m as number, d = r.depth_m as number;
    pts.push(project(x0, y0, e), project(x0 + w, y0, e), project(x0 + w, y0 + d, e), project(x0, y0 + d, e));
  }
  if (!pts.length) return <p className="muted">No room at this site has a position yet.</p>;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const pad = 3;
  const minX = Math.min(...xs) - pad - 6, maxX = Math.max(...xs) + pad, minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
  // Far levels first so nearer ones draw over them; within a level, far rooms first.
  const ordered = [...placed].sort((a, b) => elevOf(a) - elevOf(b)
    || ((a.origin_x_m as number) + (a.origin_y_m as number)) - ((b.origin_x_m as number) + (b.origin_y_m as number)));

  return (
    <svg viewBox={`${minX} ${minY} ${maxX - minX} ${maxY - minY}`} className="bld-svg" role="img"
         aria-label={`${scene.name}: ${levels.length} levels, ${placed.length} rooms`}>
      {levels.map((l) => {
        const rs = placed.filter((r) => r.level === l.name);
        if (!rs.length) return null;
        const x0 = Math.min(...rs.map((r) => r.origin_x_m as number));
        const y0 = Math.min(...rs.map((r) => r.origin_y_m as number));
        const x1 = Math.max(...rs.map((r) => (r.origin_x_m as number) + (r.width_m as number)));
        const y1 = Math.max(...rs.map((r) => (r.origin_y_m as number) + (r.depth_m as number)));
        const c = [project(x0, y0, l.elevation_m), project(x1, y0, l.elevation_m),
                   project(x1, y1, l.elevation_m), project(x0, y1, l.elevation_m)];
        const lab = project(x0, y1, l.elevation_m);
        return (
          <g key={l.name}>
            <polygon points={c.map((p) => p.join(',')).join(' ')} className="bld-level" />
            <text x={lab[0] - 1.2} y={lab[1]} className="bld-level-label">{l.name === 'Roof' ? 'Roof' : `Level ${l.name}`}</text>
          </g>
        );
      })}
      {ordered.map((r) => {
        const e = elevOf(r);
        const x0 = r.origin_x_m as number, y0 = r.origin_y_m as number, w = r.width_m as number, d = r.depth_m as number;
        const c = [project(x0, y0, e), project(x0 + w, y0, e), project(x0 + w, y0 + d, e), project(x0, y0 + d, e)];
        const cx = (c[0][0] + c[2][0]) / 2, cy = (c[0][1] + c[2][1]) / 2;
        return (
          <g key={r.id} role="button" tabIndex={0} aria-label={`${r.name}, ${r.rack_count} racks`}
             className={`bld-room${hover === r.id ? ' is-picked' : ''}${r.room_class === 'facility' ? ' is-facility' : ''}`}
             onClick={() => onEnter(r.id)} onKeyDown={(ev) => { if (ev.key === 'Enter' || ev.key === ' ') onEnter(r.id); }}
             onMouseEnter={() => onHover(r.id)} onMouseLeave={() => onHover(null)}>
            <polygon points={c.map((p) => p.join(',')).join(' ')} style={{ fill: alarmColor(r.max_severity, 0) }} />
            {w * d > 20 && <text x={cx} y={cy} className="bld-room-label">{r.name}</text>}
          </g>
        );
      })}
    </svg>
  );
}
