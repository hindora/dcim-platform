import { useMemo, useState } from 'react';
import type { TopologyNode } from '../../api/client';
import { fillOf } from './DeviceNode';

/** The left rail: everything on the canvas, one row each.
 *
 *  A device TABLE, in the simulator's form - the same columns in the same
 *  order, a dot in the type's colour against the name, the type as a pill, the
 *  addresses in mono. Two products describing one estate should not describe
 *  it two ways, and the operator reading this rail is the one who was just
 *  reading that list.
 *
 *  It is wider than the rail and scrolls sideways inside it, which is what the
 *  simulator's own list does at this width: eight facts do not fit in 250px,
 *  and the alternative - dropping the columns that do not fit - is how the
 *  list came to be missing them in the first place.
 *
 *  The list is the same graph the canvas is drawing - not a device search.
 *  Every row is a box on screen, which is what makes it useful for finding one
 *  in a hall that has been panned off the edge, and what stops it becoming a
 *  second, subtly different inventory.
 */

type SortKey = 'name' | 'type' | 'vendor' | 'mgmt' | 'prod' | 'iface'
  | 'poll' | 'where';

/** The columns, in the simulator's order and at the simulator's track widths -
 *  the same eight tracks, so the same three are in view before anybody
 *  scrolls. `table-layout: fixed`, so a long vendor name truncates in its own
 *  cell instead of dragging the rest of the row sideways.
 *
 *  Polled is the one that is wider than its counterpart: the simulator's
 *  column holds a port and this one holds `modbus:502 snmp:161`. */
const COLS: {
  key: SortKey; label: string; w: number; sortable: boolean; num?: boolean;
}[] = [
  { key: 'name',   label: 'Name',     w: 110, sortable: true },
  { key: 'type',   label: 'Type',     w: 70,  sortable: true },
  { key: 'vendor', label: 'Vendor',   w: 60,  sortable: true },
  { key: 'mgmt',   label: 'Mgmt IP',  w: 95,  sortable: true },
  { key: 'prod',   label: 'Prod IP',  w: 95,  sortable: true },
  { key: 'iface',  label: 'Iface',    w: 44,  sortable: true, num: true },
  { key: 'poll',   label: 'Polled',   w: 110, sortable: true },
  { key: 'where',  label: 'Location', w: 180, sortable: true },
];



/** `oob_switch` is not a word. The list, the filter and the pill all say it
 *  the same way or they read as three different facts. */
const typeLabel = (t: string) => t.replace(/_/g, ' ').toUpperCase();

/** DC1 · Server Hall A · R2-01, as much of it as there is. Facility gear has
 *  no rack and says so by stopping, not by printing a dash. */
function where(n: TopologyNode): string {
  return [n.location.datacenter_code, n.location.room_name, n.location.rack_name]
    .filter(Boolean).join(' · ');
}

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

/** An address sorts by its octets, not as text: `10.52.9.4` belongs before
 *  `10.52.11.2`, and a string compare puts it after. */
function ipKey(ip: string | null): string {
  if (!ip) return '￿';
  return ip.split('.').map((o) => o.padStart(3, '0')).join('.');
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
  // Type first, as the canvas is read - a hall is scanned for a KIND before it
  // is scanned for a name, and an alphabetical mix of every type answers that
  // worst. Clicking a header takes it from there.
  const [sort, setSort] = useState<SortKey>('type');
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
          || (n.vendor ?? '').toLowerCase().includes(needle)
          || (n.mgmt_ip ?? '').includes(needle)
          || (n.primary_ip ?? '').includes(needle)
          || (n.location.rack_name ?? '').toLowerCase().includes(needle)
          || (n.location.room_name ?? '').toLowerCase().includes(needle)));

    // Anything a device does not have sorts LAST whichever way the column is
    // pointing would be a lie about the order; it sorts last ascending, which
    // is what a reader means by "empty ones at the bottom".
    const last = '￿';
    const key: Record<SortKey, (n: TopologyNode) => string> = {
      name:   (n) => n.name,
      type:   (n) => n.device_type,
      vendor: (n) => n.vendor ?? last,
      mgmt:   (n) => ipKey(n.mgmt_ip),
      prod:   (n) => ipKey(n.primary_ip),
      iface:  (n) => String(n.iface_count).padStart(6, '0'),
      poll:   (n) => n.polled_on ?? last,
      where:  (n) => where(n) || last,
    };
    const k = key[sort];
    const out = matched.slice().sort((a, b) =>
      k(a).localeCompare(k(b)) || a.name.localeCompare(b.name));
    return desc ? out.reverse() : out;
  }, [nodes, q, liveType, sort, desc]);

  const sifted = Boolean(q.trim()) || Boolean(liveType);

  /** Click a header to sort by it; click it again to turn it round. */
  function onHead(k: SortKey) {
    if (k === sort) setDesc((d) => !d);
    else { setSort(k); setDesc(false); }
  }

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
               placeholder="Search name, IP, vendor" aria-label="Find a device" />
        <button type="button" className="cn-rail-hide" onClick={onToggle}
                title="Hide the panel" aria-label="Hide the panel">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
            <path d="M15 6l-6 6 6 6" />
          </svg>
        </button>
      </div>

      {/* A canvas of 155 boxes is read by kind - "just the CDUs" - long before
          it is read by name, and the search box cannot answer that without
          knowing the exact word. */}
      <div className="cn-rail-sift">
        <select value={liveType} onChange={(e) => setType(e.target.value)}
                aria-label="Device type">
          <option value="">All types</option>
          {types.map(([t, c]) => (
            <option key={t} value={t}>{typeLabel(t)} ({c})</option>
          ))}
        </select>
        <p className="cn-rail-count">
          {loading ? 'Reading…'
            : sifted ? `${rows.length} of ${nodes.length}`
            : `${nodes.length} on the canvas`}
        </p>
      </div>

      <div className="cn-rail-scroll">
        <table className="cn-rail-table">
          <colgroup>
            {COLS.map((c) => <col key={c.key} style={{ width: c.w }} />)}
          </colgroup>
          <thead>
            <tr>
              {COLS.map((c) => (
                <th key={c.key} scope="col"
                    className={[c.num ? 'right' : '',
                                sort === c.key ? 'is-sorted' : ''].filter(Boolean).join(' ')}
                    aria-sort={sort === c.key
                      ? (desc ? 'descending' : 'ascending') : undefined}>
                  {c.sortable ? (
                    <button type="button" onClick={() => onHead(c.key)}>
                      {c.label}
                      {sort === c.key && <span aria-hidden>{desc ? ' ▼' : ' ▲'}</span>}
                    </button>
                  ) : c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((n) => (
              <tr key={n.id} className={selected === n.id ? 'is-on' : undefined}
                  onClick={() => onSelect(n)}
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault(); onSelect(n);
                    }
                  }}>
                <td title={n.name}>
                  {/* Status against the name, type against the type: the two
                      facts a row is scanned for, each beside what it is about.
                      A single dot carrying both was a colour nobody could
                      read in either direction. */}
                  <span className="cn-rail-dot"
                        style={{ background: statusColor(n.status, n.max_severity) }}
                        title={n.status.toLowerCase()} />
                  {n.name}
                </td>
                <td>
                  <span className="cn-rail-type"
                        style={{ color: fillOf(n.device_type),
                                 borderColor: fillOf(n.device_type) }}>
                    {n.rolled_up > 0
                      ? `${n.rolled_up} × ${typeLabel(n.device_type)}`
                      : typeLabel(n.device_type)}
                  </span>
                </td>
                {/* A rolled-up node is a rack's worth of equipment. It has no
                    one vendor and no one address, and printing the first
                    member's would be a lie about the other nineteen. */}
                <td className="muted" title={n.vendor ?? ''}>
                  {n.rolled_up > 0 ? '—' : (n.vendor ?? '')}
                </td>
                <td className="mono" title={n.mgmt_ip ?? ''}>
                  {n.rolled_up > 0 ? '—' : (n.mgmt_ip ?? '')}
                </td>
                <td className="mono" title={n.primary_ip ?? ''}>
                  {n.rolled_up > 0 ? '—' : (n.primary_ip ?? '')}
                </td>
                <td className="mono right">
                  {n.rolled_up > 0 ? '—' : (n.iface_count || '')}
                </td>
                <td className="mono muted" title={n.polled_on ?? 'not polled'}>
                  {n.rolled_up > 0 ? '—' : (n.polled_on ?? '')}
                </td>
                <td className="muted" title={where(n)}>{where(n)}</td>
              </tr>
            ))}
            {!loading && !rows.length && (
              <tr className="cn-rail-empty">
                <td colSpan={COLS.length}>
                  {sifted ? 'Nothing here matches that.' : 'Nothing on this layer.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </aside>
  );
}
