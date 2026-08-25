/**
 * Home: the estate at a glance.
 *
 * One request behind the whole page (`/sites/overview`), because this is the
 * screen that stays open on a wall display and a dozen round trips per refresh
 * is how a NOC dashboard becomes the thing that falls over first.
 *
 * The strip is not decoration. Every number on it opens: click a counter and
 * the panel rises with the rooms behind it, searchable and exportable. So does
 * every cell in the table, scoped to that category. A headline that cannot be
 * opened is a headline an operator has to take on trust, and one they cannot
 * act on without leaving the page.
 *
 * Strip and table show the same eight categories at two resolutions. The strip
 * groups them into five counters, because it has to be readable from across a
 * room; the table keeps one column per category, so the grouping hides
 * nothing. The grouping itself comes from the server with the taxonomy - the
 * page never decides which categories belong together.
 *
 * Two numbers, kept apart everywhere they appear. The domain counters and the
 * category columns count EVERY open condition in a domain - how much is going
 * on in the plant, in the electrical chain, in our view of the estate. ALARMS
 * and the ALM column count what requires a response now.
 *
 * They are never added and never shown as if one contained the other by
 * accident: ISA-18.2 draws the line by required response, and the reason to
 * print both is that "494 open, 6 of them yours tonight" is a truer picture of
 * an estate than either number alone.
 */

import { useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type AlarmCategory,
  type AlarmCounts,
  type AlarmTaxonomy,
  type SiteRoom,
  type SiteRow,
  type SitesOverview,
} from '../../api/client';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { COLUMN_ORDER, GROUP_TONE, metaFor } from '../../components/alertMeta';
import { FacilityToggle } from '../../components/estate';
import { useColumnWidths } from '../../lib/useColumnWidths';
import { useOrg, useOrgTitle } from '../../lib/useOrg';
import { useInvalidateOn, useTopics } from '../../ws/useSocket';
import { SiteDrawer } from './SiteDrawer';
import { RoomDrawer } from './RoomDrawer';
import { AlarmLegend } from './AlarmModals';
import { AlarmPanel } from './AlarmPanel';
import { RailCards } from './RailCards';

const ALARM_EVENTS = ['alarm_created', 'alarm_updated', 'alarm_cleared'];

type Tab = 'sites' | 'rooms';

/** A header cell with a grip on its trailing edge.
 *
 *  The grip is a real control with a real hit area rather than a 1px border
 *  that happens to be draggable: a resize handle nobody can grab is a feature
 *  that only its author knows about. Double-click returns that one column to
 *  the width the page shipped with, which is faster than dragging back to a
 *  number you cannot see. */
/** What every column is rendering at right now, read off the header row. */
function measureColumns(el: HTMLElement | null): Record<string, number> {
  const row = el?.closest('tr');
  if (!row) return {};
  const out: Record<string, number> = {};
  for (const th of Array.from(row.children) as HTMLElement[]) {
    const key = th.dataset.col;
    if (key) out[key] = th.getBoundingClientRect().width;
  }
  return out;
}

function Th({ cols, col, w, className, title, children }: {
  cols: ReturnType<typeof useColumnWidths>; col: string; w: number;
  className?: string; title?: string; children: React.ReactNode;
}) {
  // `w === 0` means "take what is left". Under a fixed layout the columns
  // without a declared width share the remainder, so the table is always
  // exactly as wide as its panel until somebody drags it wider - which is the
  // difference between a scrollbar that means something and one that is just
  // the default layout missing by four pixels.
  const stored = cols.width(col, 0);
  const width = stored || w || undefined;
  const ref = useRef<HTMLTableCellElement>(null);
  return (
    <th ref={ref} data-col={col}
        className={`${className ?? ''} ${cols.resizing === col ? 'resizing' : ''}`}
        style={width ? { width } : undefined} title={title}>
      {children}
      <span className="col-grip" role="separator" aria-orientation="vertical"
            aria-label="Resize column"
            onPointerDown={cols.begin(col, w, () => measureColumns(ref.current))}
            onDoubleClick={() => cols.resetOne(col)} />
    </th>
  );
}

/** Rooms, drawn as a floor of them.
 *
 *  The control used to be a typographic `+` in a box, left over from when it
 *  expanded child rows. It navigates now, and a plus sign promises to add
 *  something - so it says what it opens instead: a plan with rooms in it. */
function RoomsGlyph({ size = 15 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden
         fill="none" stroke="currentColor" strokeWidth="1.3">
      <rect x="1.6" y="2.6" width="12.8" height="10.8" rx="1.4" />
      <line x1="6.6" y1="2.6" x2="6.6" y2="13.4" />
      <line x1="6.6" y1="8" x2="14.4" y2="8" />
    </svg>
  );
}

/** What the table filters to, and what a drill-down opens on.
 *
 *  Both are a LIST of categories rather than one, because the strip counters
 *  are groups: "Cooling & Environment" is two categories and has to filter and
 *  open as one thing. */
/** Where a click came from, and therefore what the panel may show.
 *
 *  A counter on the strip asks about the estate; a cell in DC1's row asks
 *  about DC1. Opening the same estate-wide list from both made the row's
 *  number and the panel's number disagree, which is the one thing this page
 *  cannot do - the reader clicked a 4 and was shown six rooms. */
interface Scope { kind: 'site' | 'room'; id: string; label: string }

interface Selection {
  key: string; label: string; categories: AlarmCategory[]; scope?: Scope;
  /** Opened from ALARMS or an ALM cell, so the panel is about what must be
   *  answered rather than about a domain. */
  alarmsOnly?: boolean;
}

/** Counts within a selection. Categories are mutually exclusive, so summing
 *  them is safe - the one arithmetic this page depends on. */
function countIn(alarms: AlarmCounts, categories: AlarmCategory[]): number {
  return categories.reduce((n, c) => n + (alarms.by_category?.[c] ?? 0), 0);
}

function Indicator({ category, count, alarms, label, scope, onOpen }: {
  category: AlarmCategory; count: number; alarms: number; label: string;
  scope?: Scope; onOpen: (sel: Selection) => void;
}) {
  const meta = metaFor(category);
  const on = count > 0;
  const where = scope ? ` in ${scope.label}` : '';
  return (
    <td className="ind-cell">
      {/* Lit when anything is open, and marked when some of it needs
          answering. The count is everything; the mark is the difference
          between a domain that is noisy and one that is on fire. */}
      <button className={`ind ${meta.tone} ${on ? 'on' : ''} ${alarms > 0 ? 'has-alarm' : ''}`}
              disabled={!on}
              title={on
                ? `${label}${where}: ${count} open`
                  + `${alarms > 0
                    ? `, ${alarms} needing a response`
                    : ', none needing a response'} - open the list`
                : `${label}: nothing open${where}`}
              onClick={() => onOpen({
                key: category, label, categories: [category], scope,
              })}>
        <CategoryGlyph kind={meta.glyph} />
        {on && <span>{count}</span>}
      </button>
    </td>
  );
}

/** The worst severity in a row, as a tone for its ALM tile.
 *
 *  Two more columns of numbers said the same thing as a colour and cost the
 *  width that made the table scroll sideways. The count is in the tile, the
 *  severity is the tile's colour, and the exact split is one click away in the
 *  panel - which is where somebody deciding what to do next is going anyway. */
function worst(alarms: AlarmCounts): 'critical' | 'major' | 'minor' | null {
  if (alarms.total === 0) return null;
  if (alarms.critical > 0) return 'critical';
  if (alarms.major > 0) return 'major';
  return 'minor';
}

/** ALM, then one cell per category.
 *
 *  ALM is every open alarm here and the eight cells beside it partition that
 *  number - they are the same population cut by domain, so they add up and can
 *  be read against each other. */
function IndicatorCells({ alarms, labels, scope, onOpen }: {
  alarms: AlarmCounts; labels: Record<string, string>; scope?: Scope;
  onOpen: (sel: Selection) => void;
}) {
  const n = alarms.total;
  const sev = worst(alarms);
  return (
    <>
      <td className="ind-cell">
        <button className={`ind alarms ${sev ? `on sev-${sev}` : ''}`}
                disabled={n === 0}
                title={n > 0
                  ? `${n} open alarm${n === 1 ? '' : 's'}`
                    + `${scope ? ` in ${scope.label}` : ''}: `
                    + `${alarms.critical} critical, ${alarms.major} major, `
                    + `${alarms.minor} minor`
                  : 'No open alarms'}
                onClick={() => onOpen({
                  key: 'all', label: 'Alarms', categories: [...COLUMN_ORDER],
                  scope, alarmsOnly: true,
                })}>
          <CategoryGlyph kind="alarms" />
          {n > 0 && <span>{n}</span>}
        </button>
      </td>
      {COLUMN_ORDER.map((c) => (
        <Indicator key={c} category={c} onOpen={onOpen} scope={scope}
                   label={labels[c] ?? c}
                   count={alarms.by_category?.[c] ?? 0}
                   alarms={alarms.by_category_alarms?.[c] ?? 0} />
      ))}
    </>
  );
}

export function Home() {
  const [tab, setTab] = useState<Tab>('sites');
  const [search, setSearch] = useState('');
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

  // Column widths are the reader's, not the page's: one operator watches long
  // room names, another wants the status tiles and nothing else. Remembered
  // per browser, and reset from the toolbar when a layout stops working.
  const cols = useColumnWidths('dcim.home.columns');

  // The estate's name, in the headline and in the browser tab.
  const org = useOrg();
  useOrgTitle();

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
  const { data: taxonomy } = useQuery<AlarmTaxonomy>({
    queryKey: ['alarm-taxonomy'],
    queryFn: api.alarmTaxonomy,
    staleTime: Infinity,
  });

  const labels = useMemo(() => Object.fromEntries(
    (taxonomy?.categories ?? []).map((c) => [c.key, c.label])), [taxonomy]);

  // Five grouped counters, plus the all-categories total in the middle. Every
  // category belongs to a group, so the strip and the table cover the same
  // ground at two resolutions.
  const counters = useMemo(() => {
    const groups = (taxonomy?.strip_groups ?? []).map((g) => ({
      key: g.key,
      label: g.label,
      categories: g.categories,
      tone: GROUP_TONE[g.key] ?? 'unc',
      glyph: metaFor(g.categories[0]).glyph,
    }));
    // The total sits in the middle of the strip, where the eye lands first.
    const half = Math.ceil(groups.length / 2);
    return [...groups.slice(0, half), null, ...groups.slice(half)];
  }, [taxonomy, data]);

  const sites = data?.sites ?? [];
  const rooms = useMemo(() => sites.flatMap((s) => s.rooms), [sites]);

  // Search only. Narrowing the table by category used to live on the counters;
  // the panel answers the same question better - it names the rooms, and it
  // does not make the reader hold "this table is filtered" in their head.
  const matches = (name: string) =>
    !search || name.toLowerCase().includes(search.toLowerCase());

  const visibleSites = sites.filter((s) => matches(`${s.code} ${s.name}`));
  // Unclassified rooms are never hidden: null means nobody classified it,
  // which is not the same claim as "this is plant".
  const isFacility = (r: SiteRoom) => r.room_class === 'facility';
  const facilityCount = rooms.filter(isFacility).length;
  const scopedRooms = (roomsSite
    ? rooms.filter((r) => r.datacenter_id === roomsSite.id)
    : rooms).filter((r) => showFacility || !isFacility(r));
  const visibleRooms = scopedRooms.filter(
    (r) => matches(`${r.datacenter_code} ${r.name}`));

  const rowsAll: (SiteRow | SiteRoom)[] = tab === 'sites' ? visibleSites : visibleRooms;
  const pageCount = Math.max(1, Math.ceil(rowsAll.length / pageSize));
  const current = Math.min(page, pageCount - 1);
  const rows = rowsAll.slice(current * pageSize, current * pageSize + pageSize);

  return (
    <>
      <section className="alert-strip">
        <span className="site-title">{org}</span>
        <span className="spacer" />
        <button className="caption" onClick={() => setLegendOpen(true)}
                title="What each counter counts, and what this console leaves out">
          <svg width="17" height="17" viewBox="0 0 16 16" aria-hidden
               fill="none" stroke="currentColor" strokeWidth="1.3">
            <circle cx="8" cy="8" r="6.6" />
            <line x1="8" y1="7.1" x2="8" y2="11.4" strokeLinecap="round" />
            <circle cx="8" cy="4.7" r=".85" fill="currentColor" stroke="none" />
          </svg>
          Alarm status
        </button>

        {counters.map((c) => {
          // `null` is the all-categories total, which sits between the groups.
          const total = c === null;
          const n = !data ? 0
            : total ? data.totals.total : countIn(data.totals, c.categories);
          // How much of that number is an alarm. Printed under the counter so
          // "442 open" never reads as "442 to do" - the count above is every
          // open condition in the domain, and this is the part somebody has to
          // answer. Two words, because a caption repeated six times across a
          // wall display should be read at a glance and then ignored.
          const answerable = !data || total ? 0
            : c.categories.reduce(
                (t, k) => t + (data.totals.by_category_alarms?.[k] ?? 0), 0);
          const empty = n === 0;
          return (
            <div key={total ? '__total' : c.key}
                 className={`alert-counter cat-${total ? 'alarms' : c.tone} ${empty ? 'zero' : ''}`}>
              <button className="face"
                      // Nothing to open when nothing is wrong: a panel that
                      // rises to say "no rooms" teaches an operator that the
                      // numbers are not worth clicking.
                      disabled={empty}
                      title={empty
                        ? `Nothing open in ${(total ? 'any category' : c.label.toLowerCase())}`
                        : total
                          ? 'Every open alarm, all categories - by room'
                          : `Everything open in ${c.categories
                              .map((k) => (labels[k] ?? k).toLowerCase())
                              .join(' and ')}`
                            + `, ${answerable} needing a response`}
                      onClick={() => {
                        if (empty) return;
                        setDrill(total
                          ? { key: 'all', label: 'Alarms',
                              categories: [...COLUMN_ORDER], alarmsOnly: true }
                          : { key: c.key, label: c.label, categories: c.categories });
                      }}>
                <span className="row">
                  <span className="n">{n}</span>
                  <CategoryGlyph kind={total ? 'alarms' : c.glyph} size={24} />
                </span>
                <span className="name">{total ? 'Alarms' : c.label}</span>
              </button>
              {/* Always rendered, empty when there is nothing open, so the
                  counters keep one baseline. A line that appears only on the
                  busy ones makes the strip jog up and down as the estate
                  changes, which is movement carrying no information. */}
              {!total && (
                <span className={`needs ${answerable > 0 ? 'on' : ''}`}>
                  {n === 0 ? ' '
                    : `${answerable} alarm${answerable === 1 ? '' : 's'}`}
                </span>
              )}
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
                ALL ROOMS
              </button>
            )}
            {cols.dirty && (
              <button className="row-btn" onClick={cols.reset}
                      title="Back to the column widths this page ships with">
                RESET COLUMNS
              </button>
            )}
          </div>

          {error && <div className="banner">Failed to load sites: {String(error)}</div>}

          {!!data?.unlocated_alarms && (
            <p className="muted small" style={{ margin: 0, padding: '0 22px 10px' }}>
              {data.unlocated_alarms} platform{' '}
              {data.unlocated_alarms === 1 ? 'alarm belongs' : 'alarms belong'} to no
              site, so {data.unlocated_alarms === 1 ? 'it is' : 'they are'} counted in
              the strip above but not in the table.{' '}
              <Link to="/platform">Platform health</Link>
            </p>
          )}

          {/* The site these rooms belong to: a line, not a second table.
              Stacking two tables with their own headers and their own
              scrollbars made the reader parse the chrome twice to find out
              they were looking at one site's rooms. Its numbers are one click
              away on SITES, and its KPIs are on the button. */}
          {tab === 'rooms' && roomsSite && (
            <div className="crumb">
              <button className="back"
                      onClick={() => { setRoomsSite(null); setTab('sites'); setPage(0); }}
                      aria-label={`Back to all sites from ${roomsSite.code}`}
                      title="Back to all sites">↩</button>
              <span className="name">{roomsSite.code}</span>
              {roomsSite.name !== roomsSite.code && (
                <span className="muted">{roomsSite.name}</span>
              )}
              {(roomsSite.city || roomsSite.country) && (
                <span className="muted">
                  · {[roomsSite.city, roomsSite.country].filter(Boolean).join(', ')}
                </span>
              )}
              <span className="muted">· {roomsSite.room_count} rooms</span>
              {roomsSite.alarms.total > 0 && (
                <span className="crumb-alm">
                  {roomsSite.alarms.total} open alarm{roomsSite.alarms.total === 1 ? '' : 's'}
                </span>
              )}
              <span className="spacer" />
              <button className="row-btn" onClick={() => setDrawerSite(roomsSite)}>KPIs</button>
              <Link className="row-btn primary" to={`/devices?datacenter=${roomsSite.code}`}
                    style={{ display: 'inline-block', lineHeight: '26px',
                             textAlign: 'center' }}>
                ENTER
              </Link>
            </div>
          )}

          <div className="table-scroll">
          <table className="sites-table">
            <thead>
              <tr>
                <Th cols={cols} col="name" w={0}>
                  {tab === 'sites' ? 'Site' : 'Room'}
                </Th>
                <Th cols={cols} col="second" w={170}>
                  {tab === 'sites' ? 'Location' : 'Type'}
                </Th>
                <Th cols={cols} col="count" w={65} className="ind-h">
                  {tab === 'sites' ? 'Rooms' : 'Racks'}
                </Th>
                <Th cols={cols} col="alm" w={56} className="ind-h"
                    title="Every open alarm here">ALM</Th>
                {COLUMN_ORDER.map((c) => (
                  <Th key={c} cols={cols} col={c} w={56} className="ind-h"
                      title={`${labels[c] ?? c}: everything open, alarms marked`}>
                    {metaFor(c).head}
                  </Th>
                ))}
                <Th cols={cols} col="view" w={88} className="act">View</Th>
                <Th cols={cols} col="access" w={88} className="act">Access</Th>
              </tr>
            </thead>
            <tbody>
              {isLoading && (
                <tr><td colSpan={13} className="muted" style={{ padding: 20 }}>Loading…</td></tr>
              )}

              {!isLoading && rows.length === 0 && (
                <tr>
                  <td colSpan={13} className="muted" style={{ padding: 20 }}>
                    {`No ${tab} match “${search}”.`}
                  </td>
                </tr>
              )}

              {tab === 'sites' && (rows as SiteRow[]).map((s) => (
                <SiteLine key={s.id} site={s}
                          onKpis={() => setDrawerSite(s)}
                          onRooms={() => { setRoomsSite(s); setTab('rooms'); setPage(0); }}
                          labels={labels} onDrill={setDrill} />
              ))}

              {tab === 'rooms' && (rows as SiteRoom[]).map((r) => (
                <tr key={r.id}>
                  <td>
                    <span className="site-cell">
                      <span className="room-mark" aria-hidden><RoomsGlyph /></span>
                      <span className="name">{r.name}</span>
                      {!roomsSite && <span className="muted small">{r.datacenter_code}</span>}
                    </span>
                  </td>
                  <td className="muted">{r.room_type.replace(/_/g, ' ')}</td>
                  <td className="num">{r.rack_count}</td>
                  <IndicatorCells alarms={r.alarms} labels={labels} onOpen={setDrill}
                                  scope={{ kind: 'room', id: r.id, label: r.name }} />
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

      {legendOpen && <AlarmLegend onClose={() => setLegendOpen(false)} />}
      {drill && (
        <AlarmPanel categories={drill.categories} title={drill.label}
                    scope={drill.scope} alarmsOnly={drill.alarmsOnly}
                    onClose={() => setDrill(null)} />
      )}
    </>
  );
}

/** One site.
 *
 *  The `+` used to expand the site's rooms as child rows of this table. It now
 *  switches to ROOMS scoped to this site instead: eight rooms nested under a
 *  site row pushed every other site off the screen, and the rooms arrived
 *  without the columns that make a room row worth reading - racks, type, its
 *  own way in. Same click, a view that fits. */
function SiteLine({ site, onKpis, onRooms, labels, onDrill }: {
  site: SiteRow; onKpis: () => void; onRooms: () => void;
  labels: Record<string, string>; onDrill: (sel: Selection) => void;
}) {
  return (
      <tr>
        <td>
          <span className="site-cell">
            <button className="expander" onClick={onRooms}
                    aria-label={`Show the ${site.room_count} rooms in ${site.code}`}
                    title={`Show the ${site.room_count} rooms in ${site.code}`}>
              <RoomsGlyph />
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
        <IndicatorCells alarms={site.alarms} labels={labels} onOpen={onDrill}
                        scope={{ kind: 'site', id: site.id, label: site.code }} />
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
  );
}
