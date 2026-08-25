/** The panel behind an alarm counter: which rooms, and how many in each.
 *
 *  A full-window sheet that rises from the bottom rather than a dialog in the
 *  middle. The difference is not decoration: this is a work surface an
 *  operator reads down and searches, and a centred box with a scrollbar in it
 *  makes twenty rooms feel like a peek at a list rather than the list.
 *
 *  Everything in it is the server's arithmetic. Search, sort and paging only
 *  choose which of the returned rows are on screen - none of them recompute a
 *  total, because a headline that disagrees with the rows underneath it is the
 *  failure this whole area keeps producing.
 *
 *  `scope` is the one exception, and it is the same rule seen from the other
 *  side. A cell in DC1's row asks about DC1, so the panel drops every room that
 *  is not DC1's AND re-totals from what is left - showing the estate's number
 *  over a site's rows would be the same disagreement, just louder. The rows
 *  carry their own severity and detection counts, so the facets narrow with
 *  them exactly rather than approximately.
 */

import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type AlarmCategory,
  type AlarmDetection,
  type AlarmDrillRow,
  type AlarmTaxonomy,
} from '../../api/client';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { metaFor } from '../../components/alertMeta';

const SEVERITIES = ['critical', 'major', 'minor', 'warning'] as const;

type SortKey = 'room' | 'site' | 'qty' | 'alerts' | 'devices';

/** Facet chips: the same population the rows total, split two ways. */
function Facets({ label, entries }: {
  label: string; entries: { key: string; label: string; n: number }[];
}) {
  const shown = entries.filter((e) => e.n > 0);
  if (!shown.length) return null;
  return (
    <div className="facet-row">
      <span className="facet-label">{label}</span>
      {shown.map((e) => (
        <span key={e.key} className={`facet ${e.key}`}>
          {e.label}<b>{e.n}</b>
        </span>
      ))}
    </div>
  );
}

function SortHead({ label, k, sort, dir, onSort, className }: {
  label: string; k: SortKey; sort: SortKey; dir: 1 | -1;
  onSort: (k: SortKey) => void; className?: string;
}) {
  const on = sort === k;
  return (
    <th className={className}>
      <button className={`sort ${on ? 'on' : ''}`} onClick={() => onSort(k)}
              aria-sort={on ? (dir === 1 ? 'ascending' : 'descending') : 'none'}>
        {label}<span aria-hidden>{on ? (dir === 1 ? ' ↑' : ' ↓') : ''}</span>
      </button>
    </th>
  );
}

function toCsv(rows: AlarmDrillRow[], withAlerts: boolean) {
  const head = ['Room', 'Site', 'Floor', 'Alarms',
                ...(withAlerts ? ['Alerts'] : []),
                'Devices', 'Critical', 'Major'];
  const cell = (v: unknown) => {
    const s = String(v ?? '');
    // Quote anything a spreadsheet would otherwise split or misread. Room
    // names carry commas more often than anyone expects.
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const body = rows.map((r) => [
    r.room_name, r.site_code, r.floor ?? '', r.qty,
    ...(withAlerts ? [r.alerts] : []),
    r.devices, r.critical, r.major,
  ].map(cell).join(','));
  return [head.join(','), ...body].join('\n');
}

export interface PanelScope { kind: 'site' | 'room'; id: string; label: string }

export function AlarmPanel({ categories, title, scope, alarmsOnly, onClose }: {
  categories: AlarmCategory[]; title: string; scope?: PanelScope;
  /** Opened from ALARMS or an ALM cell: the panel is about what must be
   *  answered, so the informational rows and the alert column are not in it. */
  alarmsOnly?: boolean;
  onClose: () => void;
}) {
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<SortKey>('qty');
  const [dir, setDir] = useState<1 | -1>(-1);
  const [pageSize, setPageSize] = useState(25);
  const [page, setPage] = useState(0);

  // Escape closes. A surface that covers the page and can only be dismissed
  // with the mouse traps a keyboard user.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // One query, however many categories the counter covers. Asking per category
  // and stitching the answers together in the browser gave one room two rows
  // and no honest way to count its devices - a device faulting in two domains
  // is one device, and only the database can say so.
  const { data, isLoading, error } = useQuery({
    queryKey: ['estate-alarms', ...categories],
    queryFn: () => api.estateAlarms(categories),
  });

  const { data: taxonomy } = useQuery<AlarmTaxonomy>({
    queryKey: ['alarm-taxonomy'],
    queryFn: api.alarmTaxonomy,
    staleTime: Infinity,
  });


  // Rows first, totals from the rows. Unscoped, the server's total is
  // authoritative and includes the platform alarms that belong to no room;
  // scoped, those cannot be in a site or a room by definition, so the scoped
  // total is the sum of the rows that survived.
  const inScope = (r: AlarmDrillRow) => (
    !scope ? true
      : scope.kind === 'site' ? r.site_id === scope.id
        : r.room_id === scope.id);

  // An alarms panel drops the rooms that only hold alerts: they are not what
  // it is about, and a list of forty quiet rooms buries the two that are not.
  const all: AlarmDrillRow[] = (data?.rows ?? [])
    .filter(inScope)
    .filter((r) => !alarmsOnly || r.qty > 0);

  // Everything open, matching the counter that opened this panel; and the part
  // of it somebody has to answer. Both are printed, and they are never added.
  const totalAll = scope
    ? all.reduce((n, r) => n + r.qty + r.alerts, 0)
    : (data?.total ?? 0);
  const alarms = scope
    ? all.reduce((n, r) => n + r.qty, 0)
    : (data?.alarms ?? 0);
  const total = alarmsOnly ? alarms : totalAll;
  const unlocated = scope ? 0
    : alarmsOnly ? (data?.unlocated_alarms ?? 0) : (data?.unlocated ?? 0);

  const q = search.trim().toLowerCase();
  const filtered = q
    ? all.filter((r) =>
        `${r.room_name} ${r.site_code} ${r.site_name} ${r.floor ?? ''}`
          .toLowerCase().includes(q))
    : all;

  const sorted = [...filtered].sort((a, b) => {
    const pick = (x: AlarmDrillRow) => (
      sort === 'room' ? x.room_name.toLowerCase()
        : sort === 'site' ? x.site_code.toLowerCase()
          : sort === 'devices' ? x.devices
            : sort === 'alerts' ? x.alerts
              : x.qty);
    const l = pick(a); const r = pick(b);
    if (l === r) return a.room_name.localeCompare(b.room_name);
    return (l < r ? -1 : 1) * dir;
  });

  const pageCount = Math.max(1, Math.ceil(sorted.length / pageSize));
  const current = Math.min(page, pageCount - 1);
  const rows = sorted.slice(current * pageSize, current * pageSize + pageSize);

  const severity = SEVERITIES.map((k) => ({
    key: k,
    label: k.toUpperCase(),
    n: all.reduce((n, r) => n + (r.by_severity?.[k] ?? 0), 0),
  }));
  const detections = (taxonomy?.detections ?? []).map((d) => ({
    key: d.key as AlarmDetection,
    label: d.label,
    n: all.reduce((n, r) => n + (r.by_detection?.[d.key] ?? 0), 0),
  }));

  // The alert column belongs to a panel about a domain, not to one about what
  // has to be answered - there, every row would carry a number the panel is
  // deliberately not counting.
  const withAlerts = !alarmsOnly;
  // Every category at once. Concatenating seven definitions produced a
  // paragraph nobody would read and pushed the table below the fold; one
  // sentence says the same thing.
  const everything = categories.length > 2;
  const definitions = (taxonomy?.categories ?? []).filter(
    (c) => categories.includes(c.key));
  const blurb = everything
    ? `Every open alarm ${scope ? `in ${scope.label}` : 'on the estate'}, `
      + 'grouped by the room it is in and the kind of thing that is wrong. '
      + 'Roots only: one failed uplink is one alarm, not one per device '
      + 'behind it.'
    : definitions.map((c) => c.description).join(' ');

  function onSort(k: SortKey) {
    if (k === sort) { setDir((d) => (d === 1 ? -1 : 1)); return; }
    setSort(k);
    // Counts read high-to-low, names read A-to-Z. Anything else makes the
    // first click on a column look broken.
    setDir(k === 'room' || k === 'site' ? 1 : -1);
  }

  function download() {
    const csv = toCsv(sorted, withAlerts);
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    const a = document.createElement('a');
    a.href = url;
    const where = scope ? `-${scope.label}` : '';
    a.download = `${title}${where}-alarms`
      .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') + '.csv';
    a.click();
    URL.revokeObjectURL(url);
  }

  // The all-alarms panel takes the warning triangle, not the first category's
  // glyph - a bolt over a list that is mostly visibility rows would be a lie
  // told in an icon.
  const meta = everything
    ? { tone: 'alarms', glyph: 'alarms' as const }
    : metaFor(categories[0]);

  return (
    <div className="sheet-scrim" role="dialog" aria-modal="true"
         aria-label={`${title} alarms`}
         onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="sheet">
        <header className="sheet-head">
          <div>
            <h2>
              <span className={`cat-${meta.tone}`} style={{ display: 'inline-flex' }}>
                <CategoryGlyph kind={meta.glyph} size={20} />
              </span>
              {title}{scope ? ` in ${scope.label}` : ''}:
              <span className="count"> {data ? total : '—'}</span>
              {data && !alarmsOnly && (
                <span className="of-which">
                  {alarms > 0
                    ? `${alarms} needing a response`
                    : 'none needing a response'}
                </span>
              )}
            </h2>
            {blurb && <p>{blurb}</p>}
          </div>
          <button className="close" onClick={onClose} aria-label="Close">✕</button>
        </header>

        <div className="sheet-body">
          {error && <div className="banner">Could not load the rooms behind this counter.</div>}
          {isLoading && <p className="muted">Loading…</p>}

          {data && (
            <>
              <label className="search wide">
                <span className="glass" aria-hidden />
                <input value={search} placeholder="SEARCH"
                       aria-label="Search rooms and sites"
                       onChange={(e) => { setSearch(e.target.value); setPage(0); }} />
              </label>

              <Facets label="Severity" entries={severity} />
              <Facets label="Found by" entries={detections} />
            </>
          )}

          {data && sorted.length === 0 && (
            <p className="muted">
              {q
                ? `No room matches “${search}”.`
                : `Nothing open in ${title.toLowerCase()}`
                  + `${scope ? ` in ${scope.label}` : ''}.`}
            </p>
          )}

          {sorted.length > 0 && (
            <div className="estate-scroll">
              <table className="estate-table">
                <thead>
                  <tr>
                    <SortHead label="Room" k="room" sort={sort} dir={dir} onSort={onSort} />
                    <SortHead label="Site" k="site" sort={sort} dir={dir} onSort={onSort} />
                    <th className="mid">Floor</th>
                    <SortHead label="Alarms" k="qty" sort={sort} dir={dir}
                              onSort={onSort} className="num" />
                    {withAlerts && (
                      <SortHead label="Alerts" k="alerts" sort={sort} dir={dir}
                                onSort={onSort} className="num" />
                    )}
                    <SortHead label="Devices" k="devices" sort={sort} dir={dir}
                              onSort={onSort} className="num" />
                    <th className="num">Critical</th>
                    <th className="num">Major</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.room_id}
                        className={r.critical ? 'lead-critical' : 'lead-warn'}>
                      <td>
                        <span className="name-cell"><span className="n">{r.room_name}</span></span>
                      </td>
                      <td className="muted">{r.site_code}</td>
                      <td className="mid muted">{r.floor ?? '—'}</td>
                      <td className="num"><span className="qty">{r.qty}</span></td>
                      {/* Muted on purpose. It is here to say what else is
                          going on in this room, not to compete with the
                          number somebody is acting on. */}
                      {withAlerts && (
                        <td className="num muted"
                            title={`${r.alerts} informational condition`
                                   + `${r.alerts === 1 ? '' : 's'} here - nothing `
                                   + 'that needs a response tonight'}>
                          {r.alerts || <span className="dash">—</span>}
                        </td>
                      )}
                      <td className="num">{r.devices}</td>
                      <td className="num">{r.critical || <span className="dash">—</span>}</td>
                      <td className="num">{r.major || <span className="dash">—</span>}</td>
                      <td className="num">
                        <Link className="row-btn" to={`/alarms?room=${r.room_id}`}
                              style={{ display: 'inline-block', lineHeight: '26px',
                                       textAlign: 'center' }}>
                          ENTER
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {sorted.length > 0 && (
            <div className="sheet-foot">
              <label>
                Items per page:{' '}
                <select value={pageSize}
                        onChange={(e) => { setPageSize(Number(e.target.value)); setPage(0); }}>
                  {[10, 25, 50, 100].map((n) => <option key={n} value={n}>{n}</option>)}
                </select>
              </label>
              <span>
                {current * pageSize + 1}–{Math.min(sorted.length, (current + 1) * pageSize)}
                {' of '}{sorted.length}
                {sorted.length !== all.length && ` (filtered from ${all.length})`}
              </span>
              <span className="pager">
                <button onClick={() => setPage(0)} disabled={current === 0}
                        aria-label="First page">|◀</button>
                <button onClick={() => setPage(current - 1)} disabled={current === 0}
                        aria-label="Previous page">◀</button>
                <button onClick={() => setPage(current + 1)} disabled={current >= pageCount - 1}
                        aria-label="Next page">▶</button>
                <button onClick={() => setPage(pageCount - 1)} disabled={current >= pageCount - 1}
                        aria-label="Last page">▶|</button>
              </span>
              <button className="row-btn csv" onClick={download}>DOWNLOAD CSV</button>
            </div>
          )}

          {unlocated > 0 && (
            <p className="muted small" style={{ marginTop: 14 }}>
              {unlocated} of these belong to no room — platform alarms hang off
              the pipeline rather than off a device on a floor, so they are
              counted in the total above, have no row, and are left out of the
              facets. <Link to="/platform">Platform health →</Link>
            </p>
          )}
        </div>
      </section>
    </div>
  );
}
