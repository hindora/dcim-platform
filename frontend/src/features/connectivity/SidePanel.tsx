import { useMemo, useState } from 'react';
import type { RoomSummary, TopologyNode } from '../../api/client';
import { fillOf } from './DeviceNode';

/** The left rail: what you are looking at, and everything in it.
 *
 *  The scope controls live here rather than in a popover over the canvas,
 *  because scope and contents are one question. "Which room, how far out, and
 *  what is in it" was split across a button in one corner and a diagram in the
 *  middle, so changing the room meant opening a popover, choosing, closing it,
 *  and then hunting the canvas for what had appeared.
 *
 *  The list is the same graph the canvas is drawing - not a device search.
 *  Every row is a box on screen, which is what makes it useful for finding one
 *  in a hall that has been panned off the edge, and what stops it becoming a
 *  second, subtly different inventory.
 */

const DEPTHS = [
  { value: 0, label: 'Only this room' },
  { value: 1, label: 'Also what feeds it' },
  { value: 2, label: 'Two hops out' },
];

// Short labels: these two sit in a half-width select in the rail, and a label
// the control cuts off mid-word is worse than a terse one.
const ROLLUPS = [
  { value: 'rack', label: 'Group by rack' },
  { value: 'none', label: 'Every device' },
] as const;

function statusColor(status: string, severity: string): string {
  if (status === 'OFFLINE') return 'var(--critical)';
  if (status === 'UNKNOWN') return 'var(--unknown)';
  switch (severity) {
    case 'CRITICAL': return 'var(--critical)';
    case 'MAJOR': return 'var(--major)';
    case 'MINOR':
    case 'WARNING': return 'var(--warn)';
    default: return 'var(--ok)';
  }
}

export function SidePanel({
  sites, siteId, onSite, rooms, roomId, onRoom,
  depth, onDepth, rollup, onRollup,
  nodes, selected, onSelect, open, onToggle, loading,
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
  nodes: TopologyNode[];
  selected: string | null;
  onSelect: (node: TopologyNode) => void;
  open: boolean;
  onToggle: () => void;
  loading: boolean;
}) {
  const [q, setQ] = useState('');

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const matched = needle
      ? nodes.filter((n) => n.name.toLowerCase().includes(needle)
          || n.device_type.toLowerCase().includes(needle)
          || (n.location.rack_name ?? '').toLowerCase().includes(needle))
      : nodes;
    // Grouped by type, then by name: a list of a hall's equipment is read
    // looking for a KIND first - "where are the PDUs" - and an alphabetical
    // mix of every type answers that worst.
    return matched.slice().sort((a, b) =>
      a.device_type.localeCompare(b.device_type) || a.name.localeCompare(b.name));
  }, [nodes, q]);

  if (!open) {
    return (
      <button type="button" className="cn-rail-open" onClick={onToggle}
              title="Show devices">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
          <path d="M9 6l6 6-6 6" />
        </svg>
        <span>{nodes.length}</span>
      </button>
    );
  }

  return (
    <aside className="cn-rail" aria-label="Scope and devices">
      <div className="cn-rail-scope">
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
        <div className="cn-rail-pair">
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

      <div className="cn-rail-head">
        <input value={q} onChange={(e) => setQ(e.target.value)}
               placeholder="Find a device" aria-label="Find a device" />
        <button type="button" className="cn-rail-hide" onClick={onToggle}
                title="Hide the panel" aria-label="Hide the panel">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
            <path d="M15 6l-6 6 6 6" />
          </svg>
        </button>
      </div>

      <p className="cn-rail-count">
        {loading ? 'Reading the graph…'
          : q ? `${rows.length} of ${nodes.length}`
          : `${nodes.length} on the canvas`}
      </p>

      <ul className="cn-rail-list">
        {rows.map((n) => (
          <li key={n.id}>
            <button type="button"
                    className={selected === n.id ? 'is-on' : undefined}
                    onClick={() => onSelect(n)}>
              <span className="cn-rail-chip"
                    style={{ background: fillOf(n.device_type) }} />
              <span className="cn-rail-text">
                <span className="cn-rail-name">{n.name}</span>
                <span className="cn-rail-sub">
                  {n.rolled_up > 0
                    ? `${n.rolled_up} × ${n.device_type.replace(/_/g, ' ')}`
                    : n.device_type.replace(/_/g, ' ')}
                  {n.location.rack_name && n.rolled_up === 0
                    && ` · ${n.location.rack_name}`}
                </span>
              </span>
              <span className="cn-rail-dot"
                    style={{ background: statusColor(n.status, n.max_severity) }}
                    title={n.status.toLowerCase()} />
            </button>
          </li>
        ))}
        {!loading && !rows.length && (
          <li className="cn-rail-empty">
            {q ? 'Nothing here matches that.' : 'Nothing on this layer.'}
          </li>
        )}
      </ul>
    </aside>
  );
}
