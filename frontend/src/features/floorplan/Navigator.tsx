import { useMemo } from 'react';
import type { TwinRoom, TwinSiteScene } from '../../api/client';
import { alarmColor } from './colors';

/**
 * The dock's navigator: the estate as a tree - site, then the compound
 * holding its outdoor rooms and the building, the building its levels, each
 * level its rooms - with the rung you are on lit and
 * every node a place to go. It shows the whole shape at once, which a
 * breadcrumb never did, and sits beside Search so the two ways of getting
 * somewhere are in one spot.
 *
 * Rooms carry their worst open condition as a swatch and their rack count,
 * the same facts the building panel lists, so the tree is also a glance at
 * where the trouble is.
 */

export type Rung = 'site' | 'building' | 'room';

export interface NavigatorProps {
  scene: TwinSiteScene | undefined;
  sites: { id: string; code: string; name?: string | null }[];
  siteId: string;
  rung: Rung;
  /** The level in focus at the building rung, or the open room's level. */
  level: string | null;
  roomId: string;
  onSite: (id: string) => void;
  onCompound: () => void;
  onBuilding: (level: string | null) => void;
  onRoom: (id: string) => void;
}

const OUTDOOR = 'site';
const isOutdoor = (r: TwinRoom) => (r.level ?? '').toLowerCase() === OUTDOOR;
const levelWord = (name: string) => (/^\d+$/.test(name) ? `Level ${name}` : name);

function RoomRow({ r, current, onPick }: { r: TwinRoom; current: boolean; onPick: () => void }) {
  return (
    <li>
      <button type="button" className={current ? 'is-current' : undefined} aria-current={current ? 'location' : undefined} onClick={onPick}>
        <span className="swatch" style={{ background: alarmColor(r.max_severity, 0) }} aria-hidden />
        <span className="name">{r.name}</span>
        <span className="muted">{r.rack_count ? `${r.rack_count} racks` : r.room_class === 'support' ? 'support' : r.room_class === 'outdoor' ? 'outdoor' : 'plant'}</span>
      </button>
    </li>
  );
}

export function Navigator({ scene, sites, siteId, rung, level, roomId, onSite, onCompound, onBuilding, onRoom }: NavigatorProps) {
  const levels = useMemo(() => (scene ? [...scene.levels].sort((a, b) => a.ordinal - b.ordinal) : []), [scene]);
  const rooms = scene?.rooms ?? [];
  const outdoor = rooms.filter(isOutdoor);
  const byLevel = (name: string) => rooms.filter((r) => !isOutdoor(r) && r.level === name);
  const unplaced = rooms.filter((r) => !isOutdoor(r) && !levels.some((l) => l.name === r.level));
  const site = sites.find((s) => s.id === siteId);
  const atCompound = rung === 'site';
  const atBuilding = rung === 'building' && level == null;
  return (
    <div className="vw-panel-body vw-tree">
      {sites.length > 1 && (
        <label className="vw-tree-site">
          <span className="muted">Site</span>
          <select value={siteId} onChange={(e) => onSite(e.target.value)} aria-label="Site">
            {sites.map((s) => <option key={s.id} value={s.id}>{s.code}{s.name && s.name !== s.code ? ` · ${s.name}` : ''}</option>)}
          </select>
        </label>
      )}
      <ul className="vw-tree-root">
        <li>
          <button type="button" className={`rung${atCompound ? ' is-current' : ''}`} aria-current={atCompound ? 'location' : undefined}
                  onClick={onCompound}>
            <span className="name">Compound</span>
            <span className="muted">{site?.code ?? scene?.code ?? ''}</span>
          </button>
          {/* The building stands inside the compound fence, beside the yards: its child, not its sibling. */}
          {outdoor.length > 0 && (
            <ul className="vw-tree-rooms">
              {outdoor.map((r) => <RoomRow key={r.id} r={r} current={r.id === roomId} onPick={() => onRoom(r.id)} />)}
            </ul>
          )}
          <ul className="vw-tree-levels">
            <li>
              <button type="button" className={`rung${atBuilding ? ' is-current' : ''}`} aria-current={atBuilding ? 'location' : undefined}
                      onClick={() => onBuilding(null)}>
                <span className="name">Building</span>
                <span className="muted">{levels.length ? `${levels.length} levels` : ''}</span>
              </button>
              <ul className="vw-tree-levels">
                {levels.map((l) => {
                  const here = rung === 'building' && level === l.name;
                  const rs = byLevel(l.name);
                  return (
                    <li key={l.name}>
                      <button type="button" className={`rung${here ? ' is-current' : ''}`} aria-current={here ? 'location' : undefined}
                              onClick={() => onBuilding(l.name)}>
                        <span className="name">{levelWord(l.name)}</span>
                        <span className="muted">+{l.elevation_m} m</span>
                      </button>
                      {rs.length > 0 && (
                        <ul className="vw-tree-rooms">
                          {rs.map((r) => <RoomRow key={r.id} r={r} current={r.id === roomId} onPick={() => onRoom(r.id)} />)}
                        </ul>
                      )}
                    </li>
                  );
                })}
                {unplaced.length > 0 && (
                  <li>
                    <span className="rung is-text"><span className="name">No level</span></span>
                    <ul className="vw-tree-rooms">
                      {unplaced.map((r) => <RoomRow key={r.id} r={r} current={r.id === roomId} onPick={() => onRoom(r.id)} />)}
                    </ul>
                  </li>
                )}
              </ul>
            </li>
          </ul>
        </li>
      </ul>
      <p className="muted vw-note">Backspace climbs a rung. Click a room in the picture to enter it.</p>
    </div>
  );
}
