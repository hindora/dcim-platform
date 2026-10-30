/** The device list: what is up, what is alarming, where it is.
 *
 *  The operational list of the estate, as opposed to Assets, which holds the
 *  paperwork. It answers three questions on one screen: how many devices are
 *  in the scope the reader chose and what state they are in (the header
 *  band, whose tiles narrow the list when clicked); which ones need looking
 *  at (the state and alarm columns, sortable worst-first); and where each one
 *  stands (a location trail whose parts link to the room and the rack).
 *
 *  Every filter, the sort and the page live in the URL, so a link can arrive
 *  pre-filtered - the home page's room drawer has linked to
 *  `/devices?room=…&category=cooling` since it shipped - and the band's
 *  counts are asked for under the same filters as the rows, so "716 devices"
 *  never sits above eleven of them.
 */

import { useEffect, useMemo, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import {
  api, type AssetFilterOptions, type DeviceFacets, type DeviceSummary, type Page,
} from '../../api/client';
import { Notice } from '../../components/Notice';
import { Pagination } from '../../components/Pagination';
import { PageHead, Seg, type Kpi } from '../../components/estate';
import { humanise, relativeTime, statusClass } from '../../lib/format';
import { useColumnWidths, type ColumnWidths } from '../../lib/useColumnWidths';
import { toCss } from '../../lib/zoom';
import './device-list.css';

/** Silent for this long after having answered, and a device is stale. The
 *  longest poll on the fleet is 600 s, so this is "missed one whole poll". */
const STALE_S = 600;
const PAGE_SIZE = 50;

/** The filters, and the query-string key each one uses. The keys predate
 *  this page's redesign and are linked to from elsewhere; do not rename. */
const FILTER_KEYS = ['q', 'datacenter', 'room', 'type', 'category', 'status',
                     'vendor', 'alarming', 'stale'] as const;
const VIEW_KEYS = new Set(['page', 'page_size', 'sort', 'dir']);

type SortKey = 'name' | 'device_type' | 'status' | 'alarms' | 'location'
  | 'model' | 'last_seen';

const COLUMNS: { key: SortKey | 'ip'; label: string; w: number; sort?: SortKey;
                 /** The direction a first click takes, where "ascending"
                  *  would show the uninteresting end. */
                 first?: 'asc' | 'desc'; cls?: string }[] = [
  { key: 'name', label: 'Name', w: 0, sort: 'name' },
  { key: 'device_type', label: 'Type', w: 176, sort: 'device_type' },
  { key: 'status', label: 'State', w: 104, sort: 'status' },
  { key: 'alarms', label: 'Alarms', w: 78, sort: 'alarms', first: 'desc', cls: 'num' },
  { key: 'ip', label: 'Management IP', w: 132, cls: 'col-ip' },
  { key: 'location', label: 'Location', w: 236, sort: 'location' },
  { key: 'model', label: 'Model', w: 176, sort: 'model', cls: 'col-model' },
  { key: 'last_seen', label: 'Last seen', w: 98, sort: 'last_seen', first: 'desc' },
];

const CATEGORY_LABEL: Record<string, string> = {
  power: 'Power', cooling: 'Cooling', it: 'IT', network: 'Network',
  environment: 'Environment', facility: 'Facility', other: 'Other',
};

function measureRow(th: HTMLTableCellElement | null): Record<string, number> {
  const row = th?.parentElement;
  if (!row) return {};
  const out: Record<string, number> = {};
  for (const cell of Array.from(row.children) as HTMLElement[]) {
    const key = cell.dataset.col;
    if (key) out[key] = toCss(cell.getBoundingClientRect().width);
  }
  return out;
}

function SortTh({ col, label, w, cls, sortable, cols, sort, desc, onSort }: {
  col: string; label: string; w: number; cls?: string; sortable: boolean;
  cols: ColumnWidths; sort: string; desc: boolean; onSort: () => void;
}) {
  const stored = cols.width(col, 0);
  const width = stored || w || undefined;
  const ref = useRef<HTMLTableCellElement>(null);
  const active = sort === col;
  return (
    <th ref={ref} data-col={col}
        className={`${cls ?? ''}${cols.resizing === col ? ' resizing' : ''}`}
        style={width ? { width } : undefined}
        aria-sort={active ? (desc ? 'descending' : 'ascending') : undefined}>
      {sortable ? (
        <button type="button" className="asset-sort" onClick={onSort}>
          {label}
          {active && <span className="dirn">{desc ? ' ▾' : ' ▴'}</span>}
        </button>
      ) : label}
      <span className="col-grip" role="separator" aria-orientation="vertical"
            aria-label="Resize column"
            onPointerDown={cols.begin(col, w, () => measureRow(ref.current))}
            onDoubleClick={() => cols.resetOne(col)} />
    </th>
  );
}

/** The state word, quiet when it is the expected one. Seven hundred green
 *  ONLINE chips told the reader nothing; the sixteen that are not online are
 *  what the column is for. */
function StateCell({ d }: { d: DeviceSummary }) {
  const cls = statusClass(d.status);
  const word = (
    <span className={`dl-state ${cls}`}>
      <span className="dot" aria-hidden="true" />
      {d.status.toLowerCase().replace(/_/g, ' ')}
    </span>
  );
  if (d.status !== 'NOT_MONITORED' || !d.metered_by) return word;
  // A panel has no state of its own; whether anybody is watching it is its
  // meter's state, so the meter is named here and says when it has gone quiet.
  const lost = d.metered_status !== 'ONLINE';
  const meter = d.metered_by_id
    ? <Link to={`/devices/${d.metered_by_id}`}>{d.metered_by}</Link>
    : d.metered_by;
  return (
    <span className="dl-state-wrap">
      {word}
      <span className={`dl-meter${lost ? ' lost' : ''}`}
            title={lost ? `Its meter is ${(d.metered_status ?? 'unknown').toLowerCase()}: nothing is reading this panel`
                        : 'Read through the meter on it'}>
        {lost ? <>metering lost · {meter}</> : <>via {meter}</>}
      </span>
    </span>
  );
}

function AlarmsCell({ count, severity }: { count: number; severity: string }) {
  // Nothing, not a dash: a column of placeholders is noise, and the count
  // standing alone in a row is what makes it the thing the eye lands on.
  if (!count) return <span className="sr-only">no open alarms</span>;
  const cls = statusClass(severity);
  return (
    <span className={`dl-alarms ${cls}`}
          title={`${count} open alarm${count === 1 ? '' : 's'}, worst ${severity.toLowerCase()}`}>
      <span className="dot" aria-hidden="true" />{count}
    </span>
  );
}

function LocationCell({ d }: { d: DeviceSummary }) {
  const loc = d.location;
  const parts: React.ReactNode[] = [];
  if (loc.datacenter_code) {
    parts.push(loc.datacenter_id
      ? <Link key="dc" to={`/floorplan?site=${loc.datacenter_id}`}>{loc.datacenter_code}</Link>
      : <span key="dc">{loc.datacenter_code}</span>);
  }
  if (loc.room_name) {
    parts.push(loc.room_id
      ? <Link key="rm" to={`/floorplan?room=${loc.room_id}`}>{loc.room_name}</Link>
      : <span key="rm">{loc.room_name}</span>);
  }
  if (loc.rack_name) {
    parts.push(loc.rack_id
      ? <Link key="rk" to={`/racks/${loc.rack_id}`}>{loc.rack_name}</Link>
      : <span key="rk">{loc.rack_name}</span>);
  }
  if (loc.u_start) parts.push(<span key="u" className="dl-u">U{loc.u_start}</span>);
  if (!parts.length) return <span className="dl-none">—</span>;
  return (
    <span className="dl-trail">
      {parts.map((p, i) => (
        <span key={i} className="dl-step">
          {i > 0 && <span className="dl-sep" aria-hidden="true">›</span>}{p}
        </span>
      ))}
    </span>
  );
}

function LastSeenCell({ iso }: { iso?: string | null }) {
  if (!iso) return <span className="dl-none">never</span>;
  const age = (Date.now() - new Date(iso).getTime()) / 1000;
  return (
    <span className={age > STALE_S ? 'dl-stale' : 'muted'}
          title={new Date(iso).toLocaleString()}>
      {relativeTime(iso)}
    </span>
  );
}

function SkeletonRows({ n }: { n: number }) {
  return (
    <>
      {Array.from({ length: n }, (_, i) => (
        <tr key={i} className="dl-skel" aria-hidden="true">
          {COLUMNS.map((c) => (
            <td key={c.key} className={c.cls}><span className="asset-skeleton" /></td>
          ))}
        </tr>
      ))}
    </>
  );
}

export function DeviceList() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const get = (k: string) => params.get(k) ?? '';
  const search = get('q');
  const site = get('datacenter');
  const roomId = get('room');
  const type = get('type');
  const category = get('category');
  const status = get('status');
  const vendor = get('vendor');
  const alarming = get('alarming') === '1';
  const stale = get('stale') === '1';
  const sort = get('sort');
  const desc = get('dir') === 'desc';
  const page = Math.max(1, Number(get('page') || 1));
  const pageSize = Number(get('page_size') || PAGE_SIZE);
  const cols = useColumnWidths('dcim.devices.columns');
  const searchRef = useRef<HTMLInputElement>(null);

  // `/` focuses the search, as it does on every list people spend a day in.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT'
                || t.isContentEditable)) return;
      e.preventDefault();
      searchRef.current?.focus();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  function update(changes: Record<string, string | null>, keepPage = false) {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v); else next.delete(k);
    }
    if (!keepPage) next.delete('page');
    setParams(next, { replace: true });
  }
  const set = (k: string, v: string) => update({ [k]: v || null });
  const toggle = (k: string) => update({ [k]: get(k) === '1' ? null : '1' });

  const { data: options } = useQuery<AssetFilterOptions>({
    queryKey: ['asset-filter-options'],
    queryFn: api.assetFilterOptions,
    staleTime: 5 * 60_000,
  });

  // The filters the rows and the counts share. Status is NOT in the facet
  // query: the band's tiles are the status breakdown, and a breakdown of the
  // rows already narrowed to one status would show that status alone.
  const scope: Record<string, string | undefined> = {
    search: search || undefined,
    datacenter: site || undefined,
    room_id: roomId || undefined,
    device_type: type || undefined,
    category: category || undefined,
    vendor_id: vendor || undefined,
  };
  const { data: facets } = useQuery<DeviceFacets>({
    queryKey: ['device-facets', JSON.stringify(scope)],
    queryFn: () => api.deviceFacets({ ...scope, stale_s: String(STALE_S) }),
    refetchInterval: 15_000,
  });

  const listQuery: Record<string, string | undefined> = {
    ...scope,
    status: status || undefined,
    alarming: alarming ? 'true' : undefined,
    stale_s: stale ? String(STALE_S) : undefined,
    limit: String(pageSize),
    with_total: 'true',
    offset: page > 1 ? String((page - 1) * pageSize) : undefined,
    order_by: sort || undefined,
    desc: desc ? 'true' : undefined,
  };
  const { data, error, isLoading, isFetching } = useQuery<Page<DeviceSummary>>({
    queryKey: ['devices', JSON.stringify(listQuery)],
    queryFn: () => api.devices(listQuery),
    refetchInterval: 15_000,
    placeholderData: (prev) => prev,
  });

  const typeName = useMemo(() => {
    const m: Record<string, string> = {};
    for (const t of options?.device_types ?? []) m[t.code] = t.display_name;
    return m;
  }, [options]);

  // The type menu, grouped by what the thing does, with counts under the
  // current scope so a room's menu says "CRAH (7)" and not the estate's 28.
  const typeGroups = useMemo(() => {
    const groups: Record<string, { code: string; label: string; count: number }[]> = {};
    for (const t of facets?.types ?? []) {
      (groups[t.category] ??= []).push({ code: t.code, label: t.display_name, count: t.count });
    }
    const order = ['power', 'cooling', 'it', 'network', 'environment', 'facility', 'other'];
    return order.filter((c) => groups[c]).map((c) => ({ category: c, types: groups[c] }));
  }, [facets]);

  const siteRooms = (options?.rooms ?? []).filter((r) => !site || r.dc === site);

  function pickSite(code: string) {
    // A room belongs to one site: changing the site drops a room that is not
    // in it, and keeps one that is.
    const next: Record<string, string | null> = { datacenter: code || null };
    if (roomId && !(options?.rooms ?? []).some((r) => r.id === roomId && (!code || r.dc === code))) {
      next.room = null;
    }
    update(next);
  }

  function toggleSort(key: SortKey, first: 'asc' | 'desc' = 'asc') {
    if (sort !== key) update({ sort: key, dir: first === 'desc' ? 'desc' : null });
    else if ((first === 'asc' && !desc) || (first === 'desc' && desc)) {
      update({ dir: first === 'asc' ? 'desc' : null });
    } else update({ sort: null, dir: null });
  }

  const activeFilters = FILTER_KEYS.filter((k) => params.get(k));
  const clearAll = () => {
    const next = new URLSearchParams(params);
    for (const k of [...params.keys()]) if (!VIEW_KEYS.has(k)) next.delete(k);
    next.delete('page');
    setParams(next, { replace: true });
  };

  const rows = data?.items ?? [];
  const total = data?.total ?? facets?.total ?? null;
  const staleTip = `seen before, and not in the last ${STALE_S / 60} minutes`;
  const f = facets;
  const kpis: Kpi[] = [
    { caption: 'Devices', value: f?.total ?? null, digits: 0,
      why: 'counting', tip: 'in the current scope',
      onClick: () => update({ status: null, alarming: null, stale: null }),
      active: !status && !alarming && !stale },
    { caption: 'Online', value: f?.status.online ?? null, digits: 0, tone: 'ok', why: 'counting',
      onClick: () => set('status', status === 'ONLINE' ? '' : 'ONLINE'), active: status === 'ONLINE' },
    { caption: 'Offline', value: f?.status.offline ?? null, digits: 0,
      tone: f && f.status.offline > 0 ? 'critical' : undefined, why: 'counting',
      onClick: () => set('status', status === 'OFFLINE' ? '' : 'OFFLINE'), active: status === 'OFFLINE' },
    { caption: 'Unknown', value: f?.status.unknown ?? null, digits: 0,
      tone: f && f.status.unknown > 0 ? 'warn' : undefined, why: 'counting',
      tip: 'set up for polling and never answered',
      onClick: () => set('status', status === 'UNKNOWN' ? '' : 'UNKNOWN'), active: status === 'UNKNOWN' },
    { caption: 'Not monitored', value: f?.status.not_monitored ?? null, digits: 0, why: 'counting',
      tip: 'nothing to poll: a panel read through the meter on it',
      onClick: () => set('status', status === 'NOT_MONITORED' ? '' : 'NOT_MONITORED'),
      active: status === 'NOT_MONITORED' },
    { caption: 'Alarming', value: f?.alarming ?? null, digits: 0,
      tone: f && f.alarming > 0 ? 'critical' : undefined, why: 'counting',
      tip: 'with at least one open alarm',
      onClick: () => toggle('alarming'), active: alarming },
    { caption: 'Stale', value: f?.stale ?? null, digits: 0,
      tone: f && f.stale > 0 ? 'warn' : undefined, why: 'counting', tip: staleTip,
      onClick: () => toggle('stale'), active: stale },
  ];

  const openRow = (e: React.MouseEvent, id: string) => {
    // The row is a target, but a link or button inside it wins.
    if ((e.target as HTMLElement).closest('a, button, input')) return;
    navigate(`/devices/${id}`);
  };

  return (
    <div className="dl">
      <PageHead
        title="Devices"
        sub={<>Everything the collectors watch, with its state right now. The
              paperwork is in <Link to="/assets/inventory">Assets</Link>.</>}
        kpis={kpis}
      />

      <div className="dl-tools" role="search">
        <label className="dl-search">
          <span className="sr-only">Search devices</span>
          <input ref={searchRef} type="search" value={search}
                 placeholder="Search name, IP, serial or tag  /"
                 onChange={(e) => set('q', e.target.value)} />
        </label>
        <label className="dl-pick">
          <span>Site</span>
          <select value={site} onChange={(e) => pickSite(e.target.value)}>
            <option value="">All sites</option>
            {(options?.sites ?? []).map((s) => <option key={s.id} value={s.code}>{s.code}</option>)}
          </select>
        </label>
        <label className="dl-pick">
          <span>Room</span>
          <select value={roomId} onChange={(e) => set('room', e.target.value)}>
            <option value="">All rooms</option>
            {siteRooms.map((r) => (
              <option key={r.id} value={r.id}>{site ? r.name : `${r.dc} ${r.name}`}</option>
            ))}
          </select>
        </label>
        <label className="dl-pick">
          <span>Type</span>
          <select value={type} onChange={(e) => set('type', e.target.value)}>
            <option value="">All types</option>
            {typeGroups.map((g) => (
              <optgroup key={g.category} label={CATEGORY_LABEL[g.category] ?? humanise(g.category)}>
                {g.types.map((t) => (
                  <option key={t.code} value={t.code}>{t.label} ({t.count})</option>
                ))}
              </optgroup>
            ))}
            {type && !(facets?.types ?? []).some((t) => t.code === type) && (
              <option value={type}>{typeName[type] ?? humanise(type)} (0)</option>
            )}
          </select>
        </label>
        <label className="dl-pick">
          <span>Vendor</span>
          <select value={vendor} onChange={(e) => set('vendor', e.target.value)}>
            <option value="">Any vendor</option>
            {(options?.vendors ?? []).map((v) => (
              <option key={v.id} value={v.id}>{v.name}</option>
            ))}
          </select>
        </label>
        <Seg label="State" value={status || 'ALL'}
             options={[{ key: 'ALL', label: 'ALL' }, { key: 'ONLINE', label: 'ONLINE' },
                       { key: 'DEGRADED', label: 'DEGRADED' }, { key: 'OFFLINE', label: 'OFFLINE' },
                       { key: 'UNKNOWN', label: 'UNKNOWN' },
                       { key: 'NOT_MONITORED', label: 'NOT MONITORED' }]}
             onChange={(v) => set('status', v === 'ALL' ? '' : v)} />
        <div className="seg" role="group" aria-label="Conditions">
          <button type="button" className={alarming ? 'active' : ''} aria-pressed={alarming}
                  onClick={() => toggle('alarming')}>ALARMING</button>
          <button type="button" className={stale ? 'active' : ''} aria-pressed={stale}
                  title={staleTip} onClick={() => toggle('stale')}>STALE</button>
        </div>
      </div>

      {activeFilters.length > 0 && (
        <div className="scope-strip dl-scope">
          <span className="muted">Showing</span>
          {site && <Chip onClear={() => pickSite('')}>site {site}</Chip>}
          {roomId && <Chip onClear={() => set('room', '')}>
            {(options?.rooms ?? []).find((r) => r.id === roomId)?.name ?? 'one room'}</Chip>}
          {type && <Chip onClear={() => set('type', '')}>{typeName[type] ?? humanise(type)}</Chip>}
          {category && <Chip onClear={() => set('category', '')}>
            {CATEGORY_LABEL[category] ?? humanise(category)} equipment</Chip>}
          {vendor && <Chip onClear={() => set('vendor', '')}>
            {(options?.vendors ?? []).find((v) => v.id === vendor)?.name ?? 'one vendor'}</Chip>}
          {status && <Chip onClear={() => set('status', '')}>{status.toLowerCase().replace(/_/g, ' ')}</Chip>}
          {alarming && <Chip onClear={() => toggle('alarming')}>with open alarms</Chip>}
          {stale && <Chip onClear={() => toggle('stale')}>stale</Chip>}
          {search && <Chip onClear={() => set('q', '')}>“{search}”</Chip>}
          <button type="button" className="link-button" onClick={clearAll}>clear all</button>
        </div>
      )}

      {error && (
        <Notice tone="error" action={<button type="button" onClick={() => window.location.reload()}>Retry</button>}>
          The device list could not be loaded: {String(error)}
        </Notice>
      )}

      <div className="asset-scroll dl-scroll" aria-busy={isFetching}>
        <table className="asset-table-cols dl-table">
          <thead>
            <tr>
              {COLUMNS.map((c) => (
                <SortTh key={c.key} col={c.key} label={c.label} w={c.w} cls={c.cls}
                        sortable={Boolean(c.sort)} cols={cols} sort={sort} desc={desc}
                        onSort={() => c.sort && toggleSort(c.sort, c.first)} />
              ))}
            </tr>
          </thead>
          <tbody>
            {isLoading && !data ? <SkeletonRows n={12} /> : rows.map((d) => (
              <tr key={d.id} className="dl-row" onClick={(e) => openRow(e, d.id)}>
                <td data-col="name" className="dl-name">
                  <Link to={`/devices/${d.id}`}>{d.name}</Link>
                </td>
                <td data-col="device_type" className="muted">{typeName[d.device_type] ?? humanise(d.device_type)}</td>
                <td data-col="status"><StateCell d={d} /></td>
                <td data-col="alarms" className="num">
                  <AlarmsCell count={d.active_alarms ?? 0} severity={d.max_severity} />
                </td>
                <td data-col="ip" className="mono col-ip">{d.mgmt_ip ?? d.primary_ip ?? <span className="dl-none">—</span>}</td>
                <td data-col="location"><LocationCell d={d} /></td>
                <td data-col="model" className="muted col-model" title={[d.vendor, d.model].filter(Boolean).join(' ')}>
                  {d.model ?? d.vendor ?? <span className="dl-none">—</span>}
                </td>
                <td data-col="last_seen"><LastSeenCell iso={d.last_seen} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {!isLoading && rows.length === 0 && !error && (
        <div className="asset-empty">
          {activeFilters.length > 0 ? (
            <>
              <p>No devices match these filters.</p>
              <button type="button" onClick={clearAll}>Clear filters</button>
            </>
          ) : (
            <p>No devices are recorded. Run the importer to seed the inventory.</p>
          )}
        </div>
      )}

      {(rows.length > 0 || page > 1) && (
        <Pagination
          page={page}
          pageSize={pageSize}
          shown={rows.length}
          total={total}
          hasNext={Boolean(data?.next_cursor) || (total != null && page * pageSize < total)}
          onPage={(p) => update({ page: p <= 1 ? null : String(p) }, true)}
          onSize={(s) => update({ page_size: s === PAGE_SIZE ? null : String(s) })}
          noun="devices"
        />
      )}
    </div>
  );
}

function Chip({ children, onClear }: { children: React.ReactNode; onClear: () => void }) {
  return (
    <span className="chip dl-chip">
      {children}
      <button type="button" className="dl-chip-x" aria-label="Remove this filter" onClick={onClear}>×</button>
    </span>
  );
}
