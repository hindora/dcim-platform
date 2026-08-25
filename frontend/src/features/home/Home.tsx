/**
 * Home: the estate at a glance.
 *
 * One request behind the whole page (`/sites/overview`), because this is the
 * screen that stays open on a wall display and a dozen round trips per refresh
 * is how a NOC dashboard becomes the thing that falls over first.
 *
 * The alert strip is not decoration. Each counter is a filter: clicking
 * "Power" narrows the table to the sites that have a power alert, so the
 * headline number and the rows underneath can never disagree.
 *
 * Strip and table show the same eight categories at two resolutions. The strip
 * groups them into five counters, because it has to be readable from across a
 * room; the table keeps one column per category, so the grouping hides
 * nothing. The grouping itself comes from the server with the taxonomy - the
 * page never decides which categories belong together.
 */

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type AlertCategory,
  type AlertCounts,
  type AlertTaxonomy,
  type SiteRoom,
  type SiteRow,
  type SitesOverview,
} from '../../api/client';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { COLUMN_ORDER, GROUP_TONE, metaFor } from '../../components/alertMeta';
import { FacilityToggle } from '../../components/estate';
import { useInvalidateOn, useTopics } from '../../ws/useSocket';
import { SiteDrawer } from './SiteDrawer';
import { RoomDrawer } from './RoomDrawer';
import { AlertDrilldown, AlertLegend } from './AlertModals';
import { RailCards } from './RailCards';

const ALARM_EVENTS = ['alarm_created', 'alarm_updated', 'alarm_cleared'];

type Tab = 'sites' | 'rooms';

/** What the table filters to, and what a drill-down opens on.
 *
 *  Both are a LIST of categories rather than one, because the strip counters
 *  are groups: "Cooling & Environment" is two categories and has to filter and
 *  open as one thing. */
interface Selection { key: string; label: string; categories: AlertCategory[] }

/** Counts within a selection. Categories are mutually exclusive, so summing
 *  them is safe - the one arithmetic this page depends on. */
function countIn(alerts: AlertCounts, categories: AlertCategory[]): number {
  return categories.reduce((n, c) => n + (alerts.by_category?.[c] ?? 0), 0);
}

function Indicator({ category, count, label, onOpen }: {
  category: AlertCategory; count: number; label: string;
  onOpen: (sel: Selection) => void;
}) {
  const meta = metaFor(category);
  const on = count > 0;
  return (
    <td className="ind-cell">
      <button className={`ind ${meta.tone} ${on ? 'on' : ''}`}
              disabled={!on}
              title={on ? `${label}: ${count} - list the rooms` : `${label}: 0`}
              onClick={() => onOpen({ key: category, label, categories: [category] })}>
        <CategoryGlyph kind={meta.glyph} />
        {on && <span>{count}</span>}
      </button>
    </td>
  );
}

function severityCell(n: number, tone: string) {
  return n > 0
    ? <td className="num" style={{ color: `var(--${tone})`, fontWeight: 600 }}>{n}</td>
    : <td className="num dash">—</td>;
}

/** The indicator row: every open alarm, then one cell per category.
 *
 *  ALM leads because it answers the first question - does this site have
 *  anything wrong - and it is a superset, not the sum of the cells beside it
 *  in any sense the eye needs to check: it IS their sum, since the categories
 *  partition the alarms. */
function IndicatorCells({ alerts, labels, onOpen }: {
  alerts: AlertCounts; labels: Record<string, string>;
  onOpen: (sel: Selection) => void;
}) {
  return (
    <>
      <td className="ind-cell">
        <span className={`ind alarms ${alerts.total > 0 ? 'on' : ''}`}
              title={`Open alarms, all categories: ${alerts.total}`}>
          <CategoryGlyph kind="alarms" />
          {alerts.total > 0 && <span>{alerts.total}</span>}
        </span>
      </td>
      {COLUMN_ORDER.map((c) => (
        <Indicator key={c} category={c} onOpen={onOpen}
                   label={labels[c] ?? c}
                   count={alerts.by_category?.[c] ?? 0} />
      ))}
    </>
  );
}

export function Home() {
  const [tab, setTab] = useState<Tab>('sites');
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<Selection | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [drawerSite, setDrawerSite] = useState<SiteRow | null>(null);
  const [drawerRoom, setDrawerRoom] = useState<SiteRoom | null>(null);
  const [legendOpen, setLegendOpen] = useState(false);
  const [drill, setDrill] = useState<Selection | null>(null);
  // ROOMS scoped to one site. Set by clicking a site's room count, because
  // that count provokes exactly one question - "which five rooms?" - and
  // answering it by scrolling every room in the estate is not an answer.
  const [roomsSite, setRoomsSite] = useState<SiteRow | null>(null);
  // Facility rooms - plant, switchrooms, the roof - are hidden by default.
  // They hold no racks, so on this table they are eight rows of empty
  // indicators between the halls that do carry load.
  const [showFacility, setShowFacility] = useState(false);
  const [pageSize, setPageSize] = useState(25);
  const [page, setPage] = useState(0);

  useTopics(['alarms']);
  useInvalidateOn(ALARM_EVENTS, [['sites-overview']]);

  const { data, error, isLoading } = useQuery<SitesOverview>({
    queryKey: ['sites-overview'],
    queryFn: api.sitesOverview,
    refetchInterval: 30_000,
  });

  // The taxonomy - labels, owners, and which categories group into which
  // counter. Served by the classifier that fills the counters, so the strip
  // cannot group one way while the numbers are classified another. It changes
  // only when the backend is redeployed, hence no refetch interval.
  const { data: taxonomy } = useQuery<AlertTaxonomy>({
    queryKey: ['alert-taxonomy'],
    queryFn: api.alertTaxonomy,
    staleTime: Infinity,
  });

  const labels = useMemo(() => Object.fromEntries(
    (taxonomy?.categories ?? []).map((c) => [c.key, c.label])), [taxonomy]);

  // Five grouped counters, plus the all-categories total in the middle, plus
  // `uncategorised` ONLY when it is non-zero. An empty triage bucket is not
  // news; a non-empty one is a hole in the taxonomy and has to be visible.
  const counters = useMemo(() => {
    const groups = (taxonomy?.strip_groups ?? []).map((g) => ({
      key: g.key,
      label: g.label,
      categories: g.categories,
      tone: GROUP_TONE[g.key] ?? 'unc',
      glyph: metaFor(g.categories[0]).glyph,
    }));
    const orphan = data ? (data.totals.by_category?.uncategorised ?? 0) : 0;
    if (orphan > 0) {
      groups.push({
        key: 'uncategorised', label: 'Uncategorised',
        categories: ['uncategorised'] as AlertCategory[],
        tone: 'unc', glyph: metaFor('uncategorised').glyph,
      });
    }
    // The total sits in the middle of the strip, where the eye lands first.
    const half = Math.ceil(groups.length / 2);
    return [...groups.slice(0, half), null, ...groups.slice(half)];
  }, [taxonomy, data]);

  const sites = data?.sites ?? [];
  const rooms = useMemo(() => sites.flatMap((s) => s.rooms), [sites]);

  const matches = (name: string, alerts: AlertCounts) => {
    if (search && !name.toLowerCase().includes(search.toLowerCase())) return false;
    if (filter && countIn(alerts, filter.categories) === 0) return false;
    return true;
  };

  const visibleSites = sites.filter((s) => matches(`${s.code} ${s.name}`, s.alerts));
  // Unclassified rooms are never hidden: null means nobody classified it,
  // which is not the same claim as "this is plant".
  const isFacility = (r: SiteRoom) => r.room_class === 'facility';
  const facilityCount = rooms.filter(isFacility).length;
  const scopedRooms = (roomsSite
    ? rooms.filter((r) => r.datacenter_id === roomsSite.id)
    : rooms).filter((r) => showFacility || !isFacility(r));
  const visibleRooms = scopedRooms.filter(
    (r) => matches(`${r.datacenter_code} ${r.name}`, r.alerts));

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
        <button className="caption" onClick={() => setLegendOpen(true)}
                title="What each counter counts">
          <span className="ring" /> Alert status
        </button>

        {counters.map((c) => {
          // `null` is the all-categories total, which sits between the groups.
          const total = c === null;
          const n = !data ? 0
            : total ? data.totals.total : countIn(data.totals, c.categories);
          const active = !total && filter?.key === c.key;
          return (
            <div key={total ? '__total' : c.key}
                 className={`alert-counter cat-${total ? 'alarms' : c.tone} ${n === 0 ? 'zero' : ''}`}>
              <button className="face"
                      aria-pressed={active}
                      // The total is a headline, not a facet: there is nothing
                      // to filter to when every category is already included.
                      disabled={total}
                      title={total
                        ? 'Every open alarm'
                        : `Filter the table to ${c.label.toLowerCase()}`}
                      onClick={() => {
                        if (total) return;
                        setFilter(active ? null
                          : { key: c.key, label: c.label, categories: c.categories });
                        setPage(0);
                      }}>
                <span className="row">
                  <span className="n">{n}</span>
                  <CategoryGlyph kind={total ? 'alarms' : c.glyph} size={24} />
                </span>
                <span className="name">{total ? 'Alarms' : c.label}</span>
              </button>
              {/* The counter filters the table; this lists what is behind it.
                  Two different questions - "show me only those" and "which
                  ones" - so two controls rather than one that has to guess. */}
              <button className="drill" disabled={total || n === 0}
                      title={total ? '' : `List the rooms with ${c.label.toLowerCase()}`}
                      onClick={() => {
                        if (!total) {
                          setDrill({ key: c.key, label: c.label,
                                     categories: c.categories });
                        }
                      }}>
                LIST
              </button>
              <span className="rule" />
            </div>
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
            {tab === 'rooms' && (
              <FacilityToggle on={showFacility} count={facilityCount}
                              onChange={(v) => { setShowFacility(v); setPage(0); }} />
            )}
            {roomsSite && (
              <button className="row-btn" onClick={() => { setRoomsSite(null); setPage(0); }}>
                BACK TO ALL ROOMS
              </button>
            )}
            {filter && (
              <button className="row-btn" onClick={() => setFilter(null)}
                      title={`Showing only ${tab} with an open ${filter.label.toLowerCase()} alert`}>
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
                <th>
                  {tab === 'sites' ? 'Site' : roomsSite ? `Room in ${roomsSite.code}` : 'Room'}
                </th>
                <th>{tab === 'sites' ? 'Location' : 'Type'}</th>
                <th className="ind-h">{tab === 'sites' ? 'Rooms' : 'Racks'}</th>
                <th className="ind-h" title="Open alarms, all categories">ALM</th>
                {COLUMN_ORDER.map((c) => (
                  <th key={c} className="ind-h"
                      title={`${labels[c] ?? c} alerts`}>{metaFor(c).head}</th>
                ))}
                <th className="ind-h">Crit</th>
                <th className="ind-h">Maj</th>
                <th className="act">View</th>
                <th className="act">Access</th>
              </tr>
            </thead>
            <tbody>
              {isLoading && (
                <tr><td colSpan={16} className="muted" style={{ padding: 20 }}>Loading…</td></tr>
              )}

              {!isLoading && rows.length === 0 && (
                <tr>
                  <td colSpan={16} className="muted" style={{ padding: 20 }}>
                    {filter
                      ? `No ${tab} have an open ${filter.label.toLowerCase()} alert.`
                      : `No ${tab} match “${search}”.`}
                  </td>
                </tr>
              )}

              {tab === 'sites' && (rows as SiteRow[]).map((s) => (
                <FragmentRow key={s.id} site={s} open={expanded.has(s.id)}
                             onToggle={() => toggle(s.id)}
                             onKpis={() => setDrawerSite(s)}
                             onRooms={() => { setRoomsSite(s); setTab('rooms'); setPage(0); }}
                             showFacility={showFacility}
                             labels={labels} onDrill={setDrill}
                             onRoomKpis={(r) => setDrawerRoom(r)} />
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
                  <IndicatorCells alerts={r.alerts} labels={labels} onOpen={setDrill} />
                  {severityCell(r.alerts.critical, 'critical')}
                  {severityCell(r.alerts.major, 'major')}
                  <td className="ind-cell">
                    <button className="row-btn" onClick={() => setDrawerRoom(r)}>KPIs</button>
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

      {drawerRoom && (
        <RoomDrawer roomId={drawerRoom.id} roomName={drawerRoom.name}
                    onClose={() => setDrawerRoom(null)} />
      )}

      {legendOpen && <AlertLegend onClose={() => setLegendOpen(false)} />}
      {drill && (
        <AlertDrilldown categories={drill.categories} title={drill.label}
                        onClose={() => setDrill(null)} />
      )}
    </>
  );
}

/** A site row plus, when expanded, its rooms as children of the same table. */
function FragmentRow({ site, open, onToggle, onKpis, onRooms, onRoomKpis,
                      showFacility, labels, onDrill }: {
  site: SiteRow; open: boolean; onToggle: () => void; onKpis: () => void;
  onRooms: () => void; onRoomKpis: (room: SiteRoom) => void;
  showFacility: boolean; labels: Record<string, string>;
  onDrill: (sel: Selection) => void;
}) {
  const children = showFacility
    ? site.rooms
    : site.rooms.filter((r) => r.room_class !== 'facility');
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
        <td className="num">
          {site.room_count > 0
            ? (
              <button className="linky" onClick={onRooms}
                      title={`Show the ${site.room_count} rooms in ${site.code}`}>
                {site.room_count}
              </button>
            )
            : 0}
        </td>
        <IndicatorCells alerts={site.alerts} labels={labels} onOpen={onDrill} />
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

      {open && children.map((r) => (
        <tr key={r.id} className="room-row">
          <td>
            <span className="site-cell">
              <span className="tick" aria-hidden />
              <span>{r.name}</span>
            </span>
          </td>
          <td className="muted small">{r.room_type.replace(/_/g, ' ')}</td>
          <td className="num">{r.rack_count}</td>
          <IndicatorCells alerts={r.alerts} labels={labels} onOpen={onDrill} />
          {severityCell(r.alerts.critical, 'critical')}
          {severityCell(r.alerts.major, 'major')}
          <td className="ind-cell">
            <button className="row-btn" onClick={() => onRoomKpis(r)}>KPIs</button>
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
