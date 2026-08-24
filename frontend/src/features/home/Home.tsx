/**
 * Home: the estate at a glance.
 *
 * One request behind the whole page (`/sites/overview`), because this is the
 * screen that stays open on a wall display and a dozen round trips per refresh
 * is how a NOC dashboard becomes the thing that falls over first.
 *
 * The alert strip is not decoration. Each counter is a filter: clicking
 * "Thermal" narrows the table to the sites that have a thermal alert, so the
 * headline number and the rows underneath can never disagree.
 */

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type AlertCategory,
  type AlertCounts,
  type SiteRoom,
  type SiteRow,
  type SitesOverview,
} from '../../api/client';
import { CategoryGlyph, type GlyphKind } from '../../components/CategoryGlyph';
import { useInvalidateOn, useTopics } from '../../ws/useSocket';
import { SiteDrawer } from './SiteDrawer';
import { RailCards } from './RailCards';

const ALARM_EVENTS = ['alarm_created', 'alarm_updated', 'alarm_cleared'];

type Tab = 'sites' | 'rooms';

/** The strip, left to right. `key` is null for the total. */
const COUNTERS: { key: AlertCategory | null; glyph: GlyphKind; label: string; tone: string }[] = [
  { key: 'connectivity', glyph: 'connectivity', label: 'Connectivity Alerts', tone: 'cat-connectivity' },
  { key: 'thermal', glyph: 'thermal', label: 'Thermal Alerts', tone: 'cat-thermal' },
  { key: null, glyph: 'alarms', label: 'Alarms', tone: 'cat-alarms' },
  { key: 'datapoint', glyph: 'datapoint', label: 'Datapoint Alerts', tone: 'cat-datapoint' },
  { key: 'anomaly', glyph: 'anomaly', label: 'Analytics Alerts', tone: 'cat-anomaly' },
];

/** The indicator columns, in table order. */
const INDICATORS: { key: AlertCategory | 'total'; glyph: GlyphKind; head: string; title: string }[] = [
  { key: 'thermal', glyph: 'thermal', head: 'THM', title: 'Thermal alerts' },
  { key: 'connectivity', glyph: 'connectivity', head: 'CONN', title: 'Connectivity alerts' },
  { key: 'total', glyph: 'alarms', head: 'ALM', title: 'Open alarms (all categories)' },
  { key: 'datapoint', glyph: 'datapoint', head: 'DPT', title: 'Datapoint alerts' },
  { key: 'anomaly', glyph: 'anomaly', head: 'ANL', title: 'Analytics / anomaly alerts' },
];

const TONE: Record<string, string> = {
  thermal: 'thermal', connectivity: 'connectivity', total: 'alarms',
  datapoint: 'datapoint', anomaly: 'anomaly',
};

function Indicator({ kind, count, title }: {
  kind: AlertCategory | 'total'; count: number; title: string;
}) {
  const glyph = INDICATORS.find((i) => i.key === kind)!.glyph;
  const on = count > 0;
  return (
    <td className="ind-cell">
      <span className={`ind ${TONE[kind]} ${on ? 'on' : ''}`}
            title={`${title}: ${count}`}>
        <CategoryGlyph kind={glyph} />
        {on && <span>{count}</span>}
      </span>
    </td>
  );
}

function severityCell(n: number, tone: string) {
  return n > 0
    ? <td className="num" style={{ color: `var(--${tone})`, fontWeight: 600 }}>{n}</td>
    : <td className="num dash">—</td>;
}

function indicatorCells(alerts: AlertCounts) {
  return INDICATORS.map((i) => (
    <Indicator key={i.key} kind={i.key} title={i.title}
               count={i.key === 'total' ? alerts.total : alerts[i.key]} />
  ));
}

export function Home() {
  const [tab, setTab] = useState<Tab>('sites');
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<AlertCategory | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [drawerSite, setDrawerSite] = useState<SiteRow | null>(null);
  const [pageSize, setPageSize] = useState(25);
  const [page, setPage] = useState(0);

  useTopics(['alarms']);
  useInvalidateOn(ALARM_EVENTS, [['sites-overview']]);

  const { data, error, isLoading } = useQuery<SitesOverview>({
    queryKey: ['sites-overview'],
    queryFn: api.sitesOverview,
    refetchInterval: 30_000,
  });

  const sites = data?.sites ?? [];
  const rooms = useMemo(() => sites.flatMap((s) => s.rooms), [sites]);

  const matches = (name: string, alerts: AlertCounts) => {
    if (search && !name.toLowerCase().includes(search.toLowerCase())) return false;
    if (filter && alerts[filter] === 0) return false;
    return true;
  };

  const visibleSites = sites.filter((s) => matches(`${s.code} ${s.name}`, s.alerts));
  const visibleRooms = rooms.filter((r) => matches(`${r.datacenter_code} ${r.name}`, r.alerts));

  const rowsAll: (SiteRow | SiteRoom)[] = tab === 'sites' ? visibleSites : visibleRooms;
  const pageCount = Math.max(1, Math.ceil(rowsAll.length / pageSize));
  const current = Math.min(page, pageCount - 1);
  const rows = rowsAll.slice(current * pageSize, current * pageSize + pageSize);

  function toggle(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  return (
    <>
      <section className="alert-strip">
        <span className="site-title">DCIM Platform</span>
        <span className="spacer" />
        <span className="caption"><span className="ring" /> Alert Status</span>

        {COUNTERS.map((c) => {
          const n = data
            ? (c.key === null ? data.totals.total : data.totals[c.key])
            : 0;
          const active = c.key !== null && filter === c.key;
          return (
            <button key={c.label}
                    className={`alert-counter ${c.tone} ${n === 0 ? 'zero' : ''}`}
                    aria-pressed={active}
                    // The total is a headline, not a facet: there is nothing to
                    // filter to when every category is already included.
                    disabled={c.key === null}
                    onClick={() => { setFilter(active ? null : c.key); setPage(0); }}>
              <span className="row">
                <span className="n">{n}</span>
                <CategoryGlyph kind={c.glyph} size={24} />
              </span>
              <span className="name">{c.label}</span>
              <span className="rule" />
            </button>
          );
        })}
      </section>

      <div className="home-body">
        <section className="sites-panel">
          <div className="sites-toolbar">
            <div className="tabs" style={{ margin: 0, border: 'none' }}>
              <button className={`tab ${tab === 'sites' ? 'active' : ''}`}
                      onClick={() => { setTab('sites'); setPage(0); }}>SITES</button>
              <button className={`tab ${tab === 'rooms' ? 'active' : ''}`}
                      onClick={() => { setTab('rooms'); setPage(0); }}>ROOMS</button>
            </div>
            <label className="search">
              <span className="glass" aria-hidden />
              <input value={search} placeholder="SEARCH"
                     aria-label={`Search ${tab}`}
                     onChange={(e) => { setSearch(e.target.value); setPage(0); }} />
            </label>
            {filter && (
              <button className="row-btn" onClick={() => setFilter(null)}>
                CLEAR FILTER
              </button>
            )}
          </div>

          {error && <div className="banner">Failed to load sites: {String(error)}</div>}

          {!!data?.unlocated_alerts && (
            <p className="muted small" style={{ margin: 0, padding: '0 22px 10px' }}>
              {data.unlocated_alerts} platform{' '}
              {data.unlocated_alerts === 1 ? 'alarm belongs' : 'alarms belong'} to no
              site, so {data.unlocated_alerts === 1 ? 'it is' : 'they are'} counted in
              the strip above but not in the table.{' '}
              <Link to="/platform">Platform health</Link>
            </p>
          )}

          <div className="table-scroll">
          <table className="sites-table">
            <thead>
              <tr>
                <th>{tab === 'sites' ? 'Site' : 'Room'}</th>
                <th>{tab === 'sites' ? 'Location' : 'Type'}</th>
                <th className="ind-h">{tab === 'sites' ? 'Rooms' : 'Racks'}</th>
                {INDICATORS.map((i) => (
                  <th key={i.key} className="ind-h" title={i.title}>{i.head}</th>
                ))}
                <th className="ind-h">Crit</th>
                <th className="ind-h">Maj</th>
                <th className="act">View</th>
                <th className="act">Access</th>
              </tr>
            </thead>
            <tbody>
              {isLoading && (
                <tr><td colSpan={12} className="muted" style={{ padding: 20 }}>Loading…</td></tr>
              )}

              {!isLoading && rows.length === 0 && (
                <tr>
                  <td colSpan={12} className="muted" style={{ padding: 20 }}>
                    {filter
                      ? `No ${tab} have an open ${filter} alert.`
                      : `No ${tab} match “${search}”.`}
                  </td>
                </tr>
              )}

              {tab === 'sites' && (rows as SiteRow[]).map((s) => (
                <FragmentRow key={s.id} site={s} open={expanded.has(s.id)}
                             onToggle={() => toggle(s.id)}
                             onKpis={() => setDrawerSite(s)} />
              ))}

              {tab === 'rooms' && (rows as SiteRoom[]).map((r) => (
                <tr key={r.id}>
                  <td>
                    <span className="site-cell">
                      <span className="name">{r.name}</span>
                      <span className="muted small">{r.datacenter_code}</span>
                    </span>
                  </td>
                  <td className="muted">{r.room_type.replace(/_/g, ' ')}</td>
                  <td className="num">{r.rack_count}</td>
                  {indicatorCells(r.alerts)}
                  {severityCell(r.alerts.critical, 'critical')}
                  {severityCell(r.alerts.major, 'major')}
                  <td className="ind-cell">
                    <Link className="row-btn" to={`/analytics?room=${r.id}`}
                          style={{ display: 'inline-block', lineHeight: '26px', textAlign: 'center' }}>
                      KPIs
                    </Link>
                  </td>
                  <td className="ind-cell">
                    <Link className="row-btn primary" to={`/floorplan?room=${r.id}`}
                          style={{ display: 'inline-block', lineHeight: '26px', textAlign: 'center' }}>
                      ENTER
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>

          <div className="sites-foot">
            <label>
              Items per page:{' '}
              <select value={pageSize}
                      onChange={(e) => { setPageSize(Number(e.target.value)); setPage(0); }}>
                {[10, 25, 50, 100].map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
            <span>
              {rowsAll.length === 0
                ? `0 ${tab}`
                : `${current * pageSize + 1}–${Math.min(rowsAll.length, (current + 1) * pageSize)} of ${rowsAll.length} ${tab}`}
            </span>
            <span className="pager">
              <button onClick={() => setPage(0)} disabled={current === 0} aria-label="First page">|◀</button>
              <button onClick={() => setPage(current - 1)} disabled={current === 0} aria-label="Previous page">◀</button>
              <button onClick={() => setPage(current + 1)} disabled={current >= pageCount - 1} aria-label="Next page">▶</button>
              <button onClick={() => setPage(pageCount - 1)} disabled={current >= pageCount - 1} aria-label="Last page">▶|</button>
            </span>
          </div>
        </section>

        <aside className="home-rail"><RailCards /></aside>
      </div>

      {drawerSite && (
        <SiteDrawer site={drawerSite} onClose={() => setDrawerSite(null)} />
      )}
    </>
  );
}

/** A site row plus, when expanded, its rooms as children of the same table. */
function FragmentRow({ site, open, onToggle, onKpis }: {
  site: SiteRow; open: boolean; onToggle: () => void; onKpis: () => void;
}) {
  return (
    <>
      <tr>
        <td>
          <span className="site-cell">
            <button className="expander" onClick={onToggle}
                    aria-expanded={open}
                    aria-label={`${open ? 'Collapse' : 'Expand'} ${site.code}`}>
              {open ? '−' : '+'}
            </button>
            <span className="name">{site.code}</span>
            {site.name !== site.code && <span className="muted small">{site.name}</span>}
          </span>
        </td>
        <td className="muted">
          {site.city || site.country
            ? [site.city, site.country].filter(Boolean).join(', ')
            : <span className="unset">not set</span>}
        </td>
        <td className="num">{site.room_count}</td>
        {indicatorCells(site.alerts)}
        {severityCell(site.alerts.critical, 'critical')}
        {severityCell(site.alerts.major, 'major')}
        <td className="ind-cell">
          <button className="row-btn" onClick={onKpis}>KPIs</button>
        </td>
        <td className="ind-cell">
          <Link className="row-btn primary" to={`/devices?datacenter=${site.code}`}
                style={{ display: 'inline-block', lineHeight: '26px', textAlign: 'center' }}>
            ENTER
          </Link>
        </td>
      </tr>

      {open && site.rooms.map((r) => (
        <tr key={r.id} className="room-row">
          <td>
            <span className="site-cell">
              <span className="tick" aria-hidden />
              <span>{r.name}</span>
            </span>
          </td>
          <td className="muted small">{r.room_type.replace(/_/g, ' ')}</td>
          <td className="num">{r.rack_count}</td>
          {indicatorCells(r.alerts)}
          {severityCell(r.alerts.critical, 'critical')}
          {severityCell(r.alerts.major, 'major')}
          <td className="ind-cell">
            <Link className="row-btn" to={`/analytics?room=${r.id}`}
                  style={{ display: 'inline-block', lineHeight: '24px', textAlign: 'center' }}>
              KPIs
            </Link>
          </td>
          <td className="ind-cell">
            <Link className="row-btn" to={`/floorplan?room=${r.id}`}
                  style={{ display: 'inline-block', lineHeight: '24px', textAlign: 'center' }}>
              ENTER
            </Link>
          </td>
        </tr>
      ))}
    </>
  );
}
