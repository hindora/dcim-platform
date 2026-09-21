import { useEffect, useRef } from 'react';
import type { RoomSummary } from '../../api/client';

/** What the canvas is drawing: the scope, and how much of it.
 *
 *  These four used to head the device rail, above the list. They are not the
 *  same kind of control: the rail's search and type filter narrow a LIST, and
 *  these four rebuild the GRAPH - a different room is a different drawing, not
 *  fewer rows. Stacking them together made the rail read as one long form and
 *  cost the list a third of its height before the first device.
 *
 *  So they live behind the toolbar's funnel now, on the canvas they change,
 *  and the rail is a list of what is on it. A panel rather than a popover
 *  menu: picking a room and then widening the depth is one thought, and a
 *  control that closes after every choice makes it three trips.
 */

const DEPTHS = [
  { value: 0, label: 'Only this room' },
  { value: 1, label: 'Also what feeds it' },
  { value: 2, label: 'Two hops out' },
];

const ROLLUPS = [
  { value: 'rack', label: 'Group by rack' },
  { value: 'none', label: 'Every device' },
] as const;

export function Filters({
  sites, siteId, onSite, rooms, roomId, onRoom,
  depth, onDepth, rollup, onRollup, onClose,
}: {
  sites: { id: string; code: string }[];
  siteId: string;
  onSite: (id: string) => void;
  rooms: RoomSummary[];
  roomId: string;
  onRoom: (id: string) => void;
  depth: number;
  onDepth: (d: number) => void;
  rollup: 'none' | 'rack';
  onRollup: (r: 'none' | 'rack') => void;
  onClose: () => void;
}) {
  const panel = useRef<HTMLDivElement>(null);

  // Escape closes it. A panel over a diagram has to be dismissible without
  // aiming at a 24px ✕, and the canvas underneath is what somebody wants back.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Opened from a button, so the focus is on the button; move it into the
  // panel or a keyboard lands back on the canvas behind it.
  useEffect(() => { panel.current?.querySelector('select')?.focus(); }, []);

  return (
    <div className="cn-float cn-filters" ref={panel}
         role="group" aria-label="What to draw">
      <header>
        <h3>What to draw</h3>
        <button type="button" onClick={onClose}
                title="Close" aria-label="Close the filters">✕</button>
      </header>

      <div className="cn-filters-body">
        <label>
          Site
          <select value={siteId} onChange={(e) => onSite(e.target.value)}>
            {sites.map((s) => (
              <option key={s.id} value={s.id}>{s.code}</option>
            ))}
          </select>
        </label>
        <label>
          Room
          {/* Blank is a real choice, and the first one: a site is a thing to
              look at, not a folder you have to open. Picking no room draws
              the whole site. */}
          <select value={roomId} onChange={(e) => onRoom(e.target.value)}>
            <option value="">Every room in the site</option>
            {rooms.map((r) => (
              <option key={r.id} value={r.id}>{r.name}</option>
            ))}
          </select>
        </label>
        <label>
          How far out
          <select value={depth} onChange={(e) => onDepth(Number(e.target.value))}>
            {DEPTHS.map((d) => (
              <option key={d.value} value={d.value}>{d.label}</option>
            ))}
          </select>
        </label>
        <label>
          Grouping
          <select value={rollup}
                  onChange={(e) => onRollup(e.target.value as 'none' | 'rack')}>
            {ROLLUPS.map((r) => (
              <option key={r.value} value={r.value}>{r.label}</option>
            ))}
          </select>
        </label>
      </div>
    </div>
  );
}
