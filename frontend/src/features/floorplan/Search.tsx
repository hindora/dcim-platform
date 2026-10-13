import { useEffect, useMemo, useRef, useState } from 'react';
import { humanise } from '../../lib/format';

/**
 * The dock's search: a name, and everything in the twin that answers to it.
 *
 * Rooms, racks, devices and plant, from the site scene and the open room,
 * grouped by what they are. Picking a room goes there; picking anything
 * else goes to its room and selects it. The list is the data already in
 * hand - no request is made - so it answers as fast as one can type.
 */

export type HitKind = 'room' | 'rack' | 'device' | 'unit';
export interface SearchHit {
  kind: HitKind;
  id: string;
  name: string;
  /** Where it is, in words: the room, the level, the type. */
  where: string;
  /** The room to open for it; null for a room itself. */
  roomId: string | null;
}

const ORDER: HitKind[] = ['room', 'rack', 'device', 'unit'];
const WORD: Record<HitKind, string> = { room: 'Rooms', rack: 'Racks', device: 'Devices', unit: 'Plant' };
const MAX = 60;

/** Case-folded substring match on the name and the words of where it is. Pure. */
export function matches(hit: SearchHit, q: string): boolean {
  const s = q.trim().toLowerCase();
  if (!s) return false;
  return hit.name.toLowerCase().includes(s) || hit.where.toLowerCase().includes(s);
}

export function SearchPanel({ hits, onPick }: { hits: SearchHit[]; onPick: (hit: SearchHit) => void }) {
  const [q, setQ] = useState('');
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { input.current?.focus(); }, []);
  const found = useMemo(() => {
    const out = hits.filter((h) => matches(h, q));
    // Names that start with the query come first, then the rest, each group in name order.
    const s = q.trim().toLowerCase();
    out.sort((a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind)
      || Number(b.name.toLowerCase().startsWith(s)) - Number(a.name.toLowerCase().startsWith(s))
      || a.name.localeCompare(b.name));
    return out.slice(0, MAX);
  }, [hits, q]);
  const groups = ORDER.map((k) => ({ kind: k, items: found.filter((h) => h.kind === k) })).filter((g) => g.items.length);
  const total = hits.filter((h) => matches(h, q)).length;
  return (
    <div className="vw-panel-body vw-search">
      <input ref={input} type="search" value={q} onChange={(e) => setQ(e.target.value)}
             placeholder="Room, rack, device or plant" aria-label="Search the twin"
             onKeyDown={(e) => { if (e.key === 'Enter' && found[0]) onPick(found[0]); }} />
      {q.trim() === '' ? (
        <p className="muted vw-note">Type a name. Enter opens the first match.</p>
      ) : found.length === 0 ? (
        <p className="muted vw-note">Nothing answers to “{q.trim()}”.</p>
      ) : (
        <>
          {groups.map((g) => (
            <section key={g.kind} className="vw-search-group">
              <h4>{WORD[g.kind]} <span className="muted">{g.items.length}</span></h4>
              <ul>
                {g.items.map((h) => (
                  <li key={`${h.kind}:${h.id}`}>
                    <button type="button" onClick={() => onPick(h)}>
                      <span className="name">{h.name}</span>
                      <span className="muted">{h.where}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))}
          {total > MAX && <p className="muted vw-note">{total - MAX} more - narrow it down.</p>}
        </>
      )}
    </div>
  );
}

/** The words for a device or unit type, for the `where` line. */
export const typeWord = (t: string) => humanise(t);
