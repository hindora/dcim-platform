import { useEffect, useMemo, useState } from 'react';
import type { TwinRoom, TwinSiteScene } from '../../api/client';
import { alarmColor } from './colors';
import { levelWord } from './layers';

/**
 * The dock's navigator: the estate as a tree - site, then the compound
 * holding its outdoor rooms and the building, the building its levels, each
 * level its rooms - with the rung you are on lit and
 * every node a place to go. It shows the whole shape at once, where the
 * trail at the top left shows only the one path through it, and sits beside Search so the two ways of getting
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

const FOLD_KEY = 'dcim.nav.folded';
/** 'outdoor', 'building', or 'level:<name>' - each level folds on its own. */
type Group = string;
function readFolded(): Record<Group, boolean> {
  try {
    const v = JSON.parse(localStorage.getItem(FOLD_KEY) ?? '{}');
    return v && typeof v === 'object' ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, Boolean(x)])) : {};
  } catch { return {}; }
}

/** The fold toggle of a group: a chevron button beside its row, separate
 *  from the row itself so a click on the name still navigates. */
function Fold({ open, label, onToggle }: { open: boolean; label: string; onToggle: () => void }) {
  return (
    <button type="button" className="vw-tree-fold" aria-expanded={open} aria-label={`${open ? 'Collapse' : 'Expand'} ${label}`}
            onClick={onToggle}>
      <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden style={{ transform: open ? 'rotate(90deg)' : undefined }}>
        <path d="M3 1.5 L7 5 L3 8.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </button>
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
  // Outdoor and Building fold away; each remembers it per viewer, and opens
  // itself when you navigate to somewhere inside it so the lit row is seen.
  const [folded, setFolded] = useState<Record<Group, boolean>>(readFolded);
  const toggle = (g: Group) => setFolded((f) => {
    const next = { ...f, [g]: !f[g] };
    try { localStorage.setItem(FOLD_KEY, JSON.stringify(next)); } catch { /* private window: fold for this visit only */ }
    return next;
  });
  const inOutdoor = outdoor.some((r) => r.id === roomId);
  const inBuilding = rung === 'building' || (rung === 'room' && !inOutdoor && rooms.some((r) => r.id === roomId));
  // The level you are in: the building rung's focused level, or the open room's.
  const hereLevel = rung === 'room' ? rooms.find((r) => r.id === roomId)?.level ?? null : rung === 'building' ? level : null;
  useEffect(() => {
    const open: Group[] = [];
    if (inOutdoor) open.push('outdoor');
    if (inBuilding) open.push('building');
    if (hereLevel && rung === 'room') open.push(`level:${hereLevel}`);
    if (open.some((g) => folded[g])) setFolded((f) => ({ ...f, ...Object.fromEntries(open.map((g) => [g, false])) }));
  }, [inOutdoor, inBuilding, hereLevel, rung]); // eslint-disable-line react-hooks/exhaustive-deps
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
          {/* Inside the compound fence: the outdoor yards and the building, each a group that folds. */}
          <ul className="vw-tree-levels">
            {outdoor.length > 0 && (
              <li>
                <div className="vw-tree-group">
                  <Fold open={!folded.outdoor} label="Outdoor" onToggle={() => toggle('outdoor')} />
                  <span className="rung is-text"><span className="name">Outdoor</span><span className="muted">{outdoor.length} {outdoor.length === 1 ? 'yard' : 'yards'}</span></span>
                </div>
                {!folded.outdoor && (
                  <ul className="vw-tree-rooms">
                    {outdoor.map((r) => <RoomRow key={r.id} r={r} current={r.id === roomId} onPick={() => onRoom(r.id)} />)}
                  </ul>
                )}
              </li>
            )}
            <li>
              <div className="vw-tree-group">
                <Fold open={!folded.building} label="Building" onToggle={() => toggle('building')} />
                <button type="button" className={`rung${atBuilding ? ' is-current' : ''}`} aria-current={atBuilding ? 'location' : undefined}
                        onClick={() => onBuilding(null)}>
                  <span className="name">Building</span>
                  <span className="muted">{levels.length ? `${levels.length} levels` : ''}</span>
                </button>
              </div>
              {!folded.building && <ul className="vw-tree-levels">
                {levels.map((l) => {
                  const here = rung === 'building' && level === l.name;
                  const rs = byLevel(l.name);
                  const key = `level:${l.name}`;
                  const levelBtn = (
                    <button type="button" className={`rung${here ? ' is-current' : ''}`} aria-current={here ? 'location' : undefined}
                            onClick={() => onBuilding(l.name)}>
                      <span className="name">{levelWord(l.name)}</span>
                      <span className="muted">+{l.elevation_m} m</span>
                    </button>
                  );
                  return (
                    <li key={l.name}>
                      {rs.length > 0
                        ? <div className="vw-tree-group"><Fold open={!folded[key]} label={levelWord(l.name)} onToggle={() => toggle(key)} />{levelBtn}</div>
                        : levelBtn}
                      {rs.length > 0 && !folded[key] && (
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
              </ul>}
            </li>
          </ul>
        </li>
      </ul>
      <p className="muted vw-note">Backspace climbs a rung. Click a room in the picture to enter it.</p>
    </div>
  );
}
