import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api, type TwinRoom, type TwinSiteScene } from '../../api/client';
import { useHoverTip } from '../../components/HoverTip';
import './building.css';

/**
 * A site as a building: its levels stacked, each room drawn in place on its
 * level and coloured by the worst open condition in it. A picker, not a
 * second 3D product - the reference viewer's site level chooses a room, and
 * so does this one: click a room to open it in the room viewer.
 *
 * Geometry is the import's (docs/27 Phase 0): a room's origin on its level,
 * its size, and the level's elevation. Drawn in an oblique projection so the
 * levels read as a stack without WebGL.
 */

const COS = 0.866, SIN = 0.5;        // 30-degree oblique
// Vertical drawing units per metre of elevation. Exaggerated: at 1:1 a 5 m
// floor-to-floor is less than a 12 m hall's projected depth and the levels
// overlap; at this ratio each level clears the one below.
const RISE = 2.6;

function tone(sev: string): string {
  switch (sev) {
    case 'CRITICAL': return 'var(--critical)';
    case 'MAJOR': return 'var(--major)';
    case 'MINOR':
    case 'WARNING': return 'var(--warn)';
    default: return 'var(--ok)';
  }
}

function project(x: number, y: number, elev: number): [number, number] {
  return [(x - y) * COS, (x + y) * SIN - elev * RISE];
}

export default function Building() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const { bind, tipEl } = useHoverTip();
  const [picked, setPicked] = useState<string | null>(null);
  const q = useQuery<TwinSiteScene>({
    queryKey: ['site-scene', id], queryFn: () => api.siteScene(id), enabled: Boolean(id),
    refetchInterval: 30_000,
  });
  if (q.isLoading) return <p className="muted">Loading the building…</p>;
  if (q.isError || !q.data) return <p className="muted">This site has no placed rooms yet. Import the floor plan first.</p>;
  const site = q.data;
  const placed = site.rooms.filter((r) => r.origin_x_m != null && r.origin_y_m != null && r.width_m && r.depth_m);
  const unplaced = site.rooms.filter((r) => !placed.includes(r));
  const levels = [...site.levels].sort((a, b) => a.ordinal - b.ordinal);
  const elevOf = (r: TwinRoom) => r.level_elevation_m ?? levels.find((l) => l.name === r.level)?.elevation_m ?? 0;

  // Bounds of everything drawn, for the viewBox.
  const pts: [number, number][] = [];
  for (const r of placed) {
    const e = elevOf(r);
    const x0 = r.origin_x_m as number, y0 = r.origin_y_m as number, w = r.width_m as number, d = r.depth_m as number;
    pts.push(project(x0, y0, e), project(x0 + w, y0, e), project(x0 + w, y0 + d, e), project(x0, y0 + d, e));
  }
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const pad = 3;
  const minX = Math.min(...xs) - pad - 6, maxX = Math.max(...xs) + pad, minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;

  // Far levels first so nearer ones draw over them; within a level, far rooms first.
  const ordered = [...placed].sort((a, b) => elevOf(a) - elevOf(b)
    || ((a.origin_x_m as number) + (a.origin_y_m as number)) - ((b.origin_x_m as number) + (b.origin_y_m as number)));

  return (
    <div className="stack building">
      <div className="building-head">
        <div>
          <h2>{site.name}</h2>
          <p className="subtitle">{site.code}{site.outline_m ? ` · ${levels.length} levels` : ''}
            {site.floor_to_floor_m ? ` · ${site.floor_to_floor_m} m floor to floor` : ''}</p>
        </div>
        <div className="building-links">
          <Link to="/world">World map</Link>
          <Link to={`/thermal?site=${site.datacenter_id}&scope=rooms`}>Thermal</Link>
        </div>
      </div>

      <div className="building-body">
        <div className="asset-panel building-panel">
          <svg viewBox={`${minX} ${minY} ${maxX - minX} ${maxY - minY}`} className="building-svg" role="img"
               aria-label={`${site.name}: ${levels.length} levels, ${placed.length} rooms`}>
            {levels.map((l) => {
              // Each level's footprint, from the site outline or the level's rooms.
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
                  <polygon points={c.map((p) => p.join(',')).join(' ')} className="building-level" />
                  <text x={lab[0] - 1.2} y={lab[1]} className="building-level-label">
                    {l.name === 'Roof' ? 'Roof' : `Level ${l.name}`}
                  </text>
                </g>
              );
            })}
            {ordered.map((r) => {
              const e = elevOf(r);
              const x0 = r.origin_x_m as number, y0 = r.origin_y_m as number, w = r.width_m as number, d = r.depth_m as number;
              const c = [project(x0, y0, e), project(x0 + w, y0, e), project(x0 + w, y0 + d, e), project(x0, y0 + d, e)];
              const cx = (c[0][0] + c[2][0]) / 2, cy = (c[0][1] + c[2][1]) / 2;
              const t = tone(r.max_severity);
              const go = () => navigate(`/floorplan?room=${r.id}`);
              const tipped = bind(<><b>{r.name}</b> level {r.level} · {r.rack_count} racks · {r.device_count} devices
                · {r.max_severity === 'CLEAR' ? 'no open alarms' : r.max_severity.toLowerCase()}</>);
              return (
                <g key={r.id} role="button" tabIndex={0} aria-label={`${r.name}, ${r.rack_count} racks`}
                   className={`building-room${picked === r.id ? ' is-picked' : ''}${r.room_class === 'facility' ? ' is-facility' : ''}`}
                   onClick={go} onKeyDown={(ev) => { if (ev.key === 'Enter' || ev.key === ' ') go(); }}
                   {...tipped}
                   onMouseEnter={(ev) => { tipped.onMouseEnter(ev); setPicked(r.id); }}
                   onMouseLeave={() => { tipped.onMouseLeave(); setPicked(null); }}>
                  <polygon points={c.map((p) => p.join(',')).join(' ')} style={{ fill: t }} />
                  {w * d > 20 && <text x={cx} y={cy} className="building-room-label">{r.name}</text>}
                </g>
              );
            })}
          </svg>
          {tipEl}
          <p className="muted building-note">Rooms are coloured by the worst open alarm in them. Click one to open it.</p>
        </div>

        <aside className="asset-panel building-side">
          <h3>Rooms <span className="total">{site.rooms.length}</span></h3>
          {levels.slice().reverse().map((l) => (
            <div key={l.name} className="building-level-list">
              <h4>{l.name === 'Roof' ? 'Roof' : `Level ${l.name}`} <span className="muted">+{l.elevation_m} m</span></h4>
              <ul>
                {site.rooms.filter((r) => r.level === l.name).map((r) => (
                  <li key={r.id}>
                    <button type="button" className={picked === r.id ? 'is-current' : undefined}
                            onClick={() => navigate(`/floorplan?room=${r.id}`)}
                            onMouseEnter={() => setPicked(r.id)} onMouseLeave={() => setPicked(null)}>
                      <span className="swatch" style={{ background: tone(r.max_severity) }} aria-hidden />
                      <span className="name">{r.name}</span>
                      <span className="muted">{r.rack_count ? `${r.rack_count} racks` : 'plant'}</span>
                      <span className="count">{r.device_count}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {unplaced.length > 0 && (
            <p className="muted">Not placed: {unplaced.map((r) => r.name).join(', ')} - no position in the building yet.</p>
          )}
        </aside>
      </div>
    </div>
  );
}
