import { useMemo, useState } from 'react';
import type { TopologyNode } from '../../api/client';
import { fillOf } from './DeviceNode';

/** The left rail: everything on the canvas, one row each.
 *
 *  A list, and only a list. The scope - site, room, how far out, grouping -
 *  used to head it, which made the rail a form with a list underneath and cost
 *  the list a third of its height before the first device. Those four rebuild
 *  the GRAPH rather than narrow this list, so they sit behind the toolbar's
 *  funnel now, on the canvas they change. What is left here narrows the rows:
 *  the search, the type, the order.
 *
 *  The list is the same graph the canvas is drawing - not a device search.
 *  Every row is a box on screen, which is what makes it useful for finding one
 *  in a hall that has been panned off the edge, and what stops it becoming a
 *  second, subtly different inventory.
 */

// Three orders, and the default is the one the canvas is read in: a hall's
// equipment is scanned looking for a KIND first - "where are the PDUs" - and
// an alphabetical mix of every type answers that worst.
const SORTS = [
  { value: 'type', label: 'Type' },
  { value: 'name', label: 'Name' },
  { value: 'rack', label: 'Rack' },
] as const;

type Sort = typeof SORTS[number]['value'];

/** `oob_switch` is not a word. The list, the filter and the chip all say it
 *  the same way or they read as three different facts. */
const typeLabel = (t: string) => t.replace(/_/g, ' ').toUpperCase();

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
  nodes, selected, onSelect, open, onToggle, loading,
}: {
  nodes: TopologyNode[];
  selected: string | null;
  onSelect: (node: TopologyNode) => void;
  open: boolean;
  onToggle: () => void;
  loading: boolean;
}) {
  const [q, setQ] = useState('');
  const [type, setType] = useState('');
  const [sort, setSort] = useState<Sort>('type');
  const [desc, setDesc] = useState(false);

  /** What is actually on the canvas, with counts. Built from the nodes rather
   *  than from a fixed list of every type the estate knows, because a filter
   *  offering CHILLER on a network layer is offering nothing. */
  const types = useMemo(() => {
    const by = new Map<string, number>();
    for (const n of nodes) by.set(n.device_type, (by.get(n.device_type) ?? 0) + 1);
    return [...by.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [nodes]);

  // Changing layer or room can take the filtered type off the canvas. Read as
  // "all" when that happens rather than showing an empty list with a filter
  // naming something that is no longer there.
  const liveType = types.some(([t]) => t === type) ? type : '';

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const matched = nodes.filter((n) =>
      (!liveType || n.device_type === liveType)
      && (!needle
          || n.name.toLowerCase().includes(needle)
          || n.device_type.toLowerCase().includes(needle)
          || (n.location.rack_name ?? '').toLowerCase().includes(needle)));

    // Racks last when there is no rack: a facility machine has no elevation,
    // and sorting it under the empty string puts the plant above every rack.
    const byRack = (n: TopologyNode) => n.location.rack_name ?? '￿';
    const cmp = sort === 'name'
      ? (a: TopologyNode, b: TopologyNode) => a.name.localeCompare(b.name)
      : sort === 'rack'
        ? (a: TopologyNode, b: TopologyNode) =>
            byRack(a).localeCompare(byRack(b)) || a.name.localeCompare(b.name)
        : (a: TopologyNode, b: TopologyNode) =>
            a.device_type.localeCompare(b.device_type) || a.name.localeCompare(b.name);

    const out = matched.slice().sort(cmp);
    return desc ? out.reverse() : out;
  }, [nodes, q, liveType, sort, desc]);

  const sifted = Boolean(q.trim()) || Boolean(liveType);

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
    <aside className="cn-rail" aria-label="Devices on the canvas">
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

      {/* Type and order on one line under the search. A canvas of 155 boxes is
          read by kind - "just the CDUs" - long before it is read by name, and
          the search box cannot answer that without knowing the exact word. */}
      <div className="cn-rail-sift">
        <select value={liveType} onChange={(e) => setType(e.target.value)}
                aria-label="Device type">
          <option value="">All types</option>
          {types.map(([t, c]) => (
            <option key={t} value={t}>{typeLabel(t)} ({c})</option>
          ))}
        </select>
        <select value={sort} onChange={(e) => setSort(e.target.value as Sort)}
                aria-label="Sort by">
          {SORTS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <button type="button" className="cn-rail-dir"
                onClick={() => setDesc((d) => !d)}
                title={desc ? 'Descending' : 'Ascending'}
                aria-label={desc ? 'Sort descending' : 'Sort ascending'}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"
               strokeLinejoin="round" aria-hidden>
            <path d={desc ? 'M6 9l6 6 6-6' : 'M6 15l6-6 6 6'} />
          </svg>
        </button>
      </div>

      <p className="cn-rail-count">
        {loading ? 'Reading the graph…'
          : sifted ? `${rows.length} of ${nodes.length}`
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
                  {/* The type in the type's own colour, as the canvas fills
                      the box with it - so a row and its node are the same
                      fact twice, not two. */}
                  <span className="cn-rail-type"
                        style={{ color: fillOf(n.device_type),
                                 borderColor: fillOf(n.device_type) }}>
                    {n.rolled_up > 0
                      ? `${n.rolled_up} × ${typeLabel(n.device_type)}`
                      : typeLabel(n.device_type)}
                  </span>
                  {n.location.rack_name && n.rolled_up === 0
                    && <span className="cn-rail-where">{n.location.rack_name}</span>}
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
            {sifted ? 'Nothing here matches that.' : 'Nothing on this layer.'}
          </li>
        )}
      </ul>
    </aside>
  );
}
