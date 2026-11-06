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

import { Fragment, useEffect, useRef, useState } from 'react';
import { keepPreviousData, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type Alarm,
  type AlarmCategory,
  type AlarmDetection,
  type AlarmDrill,
  type AlarmDrillRow,
  type AlarmTaxonomy,
  type TrendWindow,
} from '../../api/client';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { StatusChip } from '../../components/StatusChip';
import { Tip } from '../../components/HoverTip';
import { AlarmTrend, MAX_DAYS, TrendGlyph, daysBetween, isoDay, maxOpen } from './AlarmTrend';
import { Seg } from '../../components/estate';
import { AlarmDetailModal } from '../alarms/AlarmDetail';
import { Pagination } from '../../components/Pagination';
import { metaFor } from '../../components/alertMeta';
import { humanise, relativeTime } from '../../lib/format';
import { Notice } from '../../components/Notice';

const SEVERITIES = ['critical', 'major', 'minor', 'warning'] as const;

/** The words an operator uses for a condition.
 *
 *  `cpu_temp_high` reads as "CPU temp high", not "Cpu temp high": every one of
 *  these names is half acronym, and sentence-casing them wholesale makes a
 *  column of familiar terms look like a column of typos.
 */

/** An endpoint id is an instance to the database and noise to a person.
 *
 *  `endpoint_unreachable` carries the endpoint's UUID, which identifies the
 *  row perfectly and tells an operator nothing. A port name or a sensor index
 *  is worth showing; 36 hex characters are not. */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const isUuid = (s: string) => UUID.test(s);

const UNITS: Record<string, string> = {
  cpu_temperature: '\u00b0C', inlet_temperature: '\u00b0C',
  exhaust_temperature: '\u00b0C', ambient_temperature: '\u00b0C',
  supply_air_temp: '\u00b0C', return_air_temp: '\u00b0C',
  cpu_utilization: '%', memory_utilization: '%', disk_utilization: '%',
  load_pct: '%', relative_humidity: '%', power_factor: '',
  power_draw: ' W', apparent_power: ' VA', current: ' A',
  voltage_ln: ' V', voltage_ll: ' V', line_frequency: ' Hz',
};

/** The measurement behind a threshold alarm.
 *
 *  Returns null when the alarm carries no reading - a link down or a trap has
 *  nothing to put here, and an empty cell beats a fabricated one.
 */
function reading(a: Alarm): string | null {
  if (a.trigger_value === null || a.trigger_value === undefined) return null;
  const unit = UNITS[a.metric_key ?? ''] ?? '';
  let value = `${Math.round(a.trigger_value * 10) / 10}${unit}`;
  // A percentage of nameplate is the number to act on; the absolute draw is
  // the one to size against. A PDU at 85% means nothing to somebody who does
  // not carry the strip's rating in their head, so both go on the row.
  if (a.metric_key === 'load_pct' && a.trigger_va != null) {
    const kva = a.trigger_va / 1000;
    value += ` (${kva >= 10 ? Math.round(kva) : Math.round(kva * 10) / 10} kVA)`;
  }
  if (a.threshold === null || a.threshold === undefined) return value;
  return `${value}, limit ${Math.round(a.threshold * 10) / 10}${unit}`;
}

/** Does the message add anything the Condition column has not already said?
 *
 *  Trap messages used to be `cpu_high_usage (1.3.6.1.4.1.99999.1.1)` - the
 *  condition again, in machine vocabulary, plus an OID. Two columns saying one
 *  thing is worse than one column: the eye reads both before discovering the
 *  second was redundant.
 */
function addsSomething(message: string, alarmType: string) {
  const normal = (t: string) => t.toLowerCase().replace(/[^a-z0-9]+/g, '');
  const m = normal(message);
  return m !== '' && m !== normal(alarmType) && m !== normal(humanise(alarmType));
}

/** One line of detail: the words, carrying the reading where the words lack it.
 *
 *  A rule writes its own sentence and usually puts the numbers in it - "CPU
 *  temperature 93.0 C above 80.0 C" needs nothing added, and a numeric column
 *  beside that was the same fact twice. But the sentences are not uniform:
 *  `cpu_temp_critical` says "93.0 C critical" and never mentions the 90 it
 *  crossed, and a trap says only what it is.
 *
 *  So: the message when it says something, the limit appended when the message
 *  states a value without what it breached, and the bare reading when there are
 *  no words worth showing at all.
 */
function conditionDetail(a: Alarm): string | null {
  const words = addsSomething(a.message, a.alarm_type) ? a.message : '';
  const measure = reading(a);
  if (!measure) return words || null;
  if (!words) return measure;

  const mentions = (n: number) =>
    a.message.includes(String(Math.round(n * 10) / 10))
    || a.message.includes(String(Math.round(n)));
  if (a.trigger_value != null && !mentions(a.trigger_value)) {
    return `${words} · ${measure}`;
  }
  if (a.threshold != null && !mentions(a.threshold)) {
    const unit = UNITS[a.metric_key ?? ''] ?? '';
    return `${words} (limit ${Math.round(a.threshold * 10) / 10}${unit})`;
  }
  return words;
}

/** What the operator is actually about to touch.
 *
 *  An outlet condition names a receptacle, and a receptacle number is a
 *  position on a strip rather than a thing anybody recognises: "Outlet 31" says
 *  where to put your hand, not what goes dark when you do. The cord is modelled
 *  end to end, so the load is already known - and on a rack PDU that load is the
 *  whole reason the alarm matters. A dead outlet feeding nothing is a note for
 *  the next maintenance window; the same outlet feeding a CDU is not.
 *
 *  Kept ahead of the reading, because "which machine" is the first question and
 *  the amps are the second.
 */
function detail(a: Alarm): string | null {
  const rest = conditionDetail(a);
  if (!a.instance_feeds) return rest;
  const feeds = `feeds ${a.instance_feeds}`;
  return rest ? `${feeds} · ${rest}` : feeds;
}


/** The conditions inside one room, alarms above alerts.
 *
 *  Fetched when the row is opened, not with the panel: forty rooms times their
 *  conditions is a payload nobody asked for, and the panel's first job is to be
 *  readable immediately.
 *
 *  Both classes, always - including inside an alarms-only panel. Once a row is
 *  open the question has changed from "what must I answer" to "what is actually
 *  wrong in this room", and the alert that has not crossed its threshold yet is
 *  usually the context for the alarm that has.
 */
/** Which population the whole panel is about.
 *
 *  `live` is what the counter that opened the panel is counting - a
 *  drill-down that disagreed with the number above it would be worse than no
 *  drill-down. `history` is the same rooms with everything they have raised,
 *  open and cleared alike: what has this room been doing, and did the thing I
 *  fixed actually clear. One switch, held at the top, so every row, count and
 *  expansion under a headline describes the same population as the headline.
 *  A per-row lifecycle switch used to sit inside the expansion, and it put a
 *  cleared list under an open count.
 */
type Tab = 'live' | 'history';

const HISTORY_STATES = ['ACTIVE', 'ACKNOWLEDGED', 'CLEARED'];

/** Rows per page inside an expanded room, to start with. A hall's history
 *  runs to thousands of conditions; they are all fetched, but not all
 *  mounted at once - nine thousand table rows in a sub-row make the whole
 *  sheet drag. The pager is the inventory table's, so the reader has the
 *  same range, rows-per-page, go-to and page numbers here as there. */
const SUB_PAGE = 25;

/** The pressed facet chips, as the API takes them - every key is the
 *  query parameter it becomes. */
interface Facets {
  severity: string[]; detection: string[]; device_type: string[];
  response_class: string[]; state: string[]; standing: string[]; alarm_type: string[];
}
const NO_FACETS: Facets = {
  severity: [], detection: [], device_type: [],
  response_class: [], state: [], standing: [], alarm_type: [],
};
const FACET_KEYS = Object.keys(NO_FACETS) as (keyof Facets)[];
const facetsActive = (f: Facets) => FACET_KEYS.some((k) => f[k].length > 0);
/** The chips as part of a query key, so a change to any row refetches. */
const facetKey = (f: Facets) => FACET_KEYS.flatMap((k) => [k, ...f[k]]);

/** How far back the history tab looks. Daily bars up to a quarter; past
 *  that the chart draws weeks. CUSTOM is two dates the operator picks. */
const WINDOWS = [
  { label: '7D', days: 7, said: 'in the last 7 days' },
  { label: '30D', days: 30, said: 'in the last 30 days' },
  { label: '90D', days: 90, said: 'in the last 90 days' },
  { label: '1Y', days: 365, said: 'in the last year' },
] as const;
const CUSTOM = 'CUSTOM';
type Preset = typeof WINDOWS[number]['label'] | typeof CUSTOM;
const shortDay = (iso: string) => iso.slice(5);

/** The three fixed vocabularies, in the words the chips wear. */
const STANDINGS = [
  { key: 'cause', label: 'Cause',
    tip: 'Has other conditions folded under it: answer this one first' },
  { key: 'consequence', label: 'Consequence',
    tip: 'Folded under a cause elsewhere: answered by answering that' },
  { key: 'alone', label: 'Stands alone', tip: 'Neither a cause nor a consequence' },
];
const STATES = [
  { key: 'active', label: 'Unacknowledged', tip: 'Nobody has acknowledged it yet' },
  { key: 'acknowledged', label: 'Acknowledged', tip: 'Somebody has it' },
  { key: 'cleared', label: 'Cleared', tip: 'Over; here for the record' },
];


/** How many pages past the one on screen are fetched ahead of a click. */
const LOOKAHEAD = 2;

/** What a room's list asks for. One page at a time, from the server, in
 *  the server's order; the row above says how many there are. A hall's
 *  history runs to thousands of conditions, and fetching them all to show
 *  twenty-five dragged the sheet.
 *
 *  `version` is the row's `changed_at`: when the room's list last changed
 *  shape. It is in the key, so re-opening an unchanged room reads the
 *  cache and costs nothing, and a room that raised, cleared or
 *  acknowledged something since is fetched afresh. On history it also
 *  freezes the walk: pages are read as of that instant, so nothing landing
 *  meanwhile can shift them, and a landing bumps the version instead. The
 *  live list is not frozen, because there a new condition is the point.
 *
 *  One builder, used by the list and by the hover that primes it, so the
 *  two name the same cache entry. */
interface SubList {
  roomId: string; categories: AlarmCategory[]; tab: Tab; facets: Facets;
  window?: TrendWindow; version: string | null;
}
/** The instant just after a version, as the exclusive `before` bound: the
 *  server keeps microseconds, the browser milliseconds, so a millisecond
 *  past the truncated value is past the true one too. */
const justAfter = (iso: string) => new Date(Date.parse(iso) + 1).toISOString();
function subListQuery(q: SubList, p: number, s: number) {
  const history = q.tab === 'history';
  const before = history && q.version ? justAfter(q.version) : undefined;
  return {
    queryKey: [
      'room-conditions', q.roomId, q.tab, ...q.categories, ...facetKey(q.facets),
      q.window?.since ?? '', q.window?.until ?? '', q.version ?? '', s, p],
    queryFn: () => api.roomConditions(
      q.roomId, q.categories, history ? HISTORY_STATES : undefined, p * s,
      facetsActive(q.facets) ? q.facets : undefined, true, q.window,
      { limit: s, before, order: history ? 'recent' : 'triage' }),
    staleTime: 15_000,
  };
}

function RoomConditions({ roomId, categories, span, tab, facets, window, version, counts, onOpen }: {
  roomId: string; categories: AlarmCategory[]; span: number; tab: Tab;
  facets: Facets; window?: TrendWindow;
  /** The row's `changed_at` (see subListQuery). */
  version: string | null;
  /** The row's own figures, which this list is a page of. */
  counts: { alarms: number; alerts: number };
  onOpen: (a: Alarm) => void;
}) {
  const history = tab === 'history';
  const qc = useQueryClient();
  const [page, setPage] = useState(0);
  const [size, setSize] = useState(SUB_PAGE);
  const q: SubList = { roomId, categories, tab, facets, window, version };
  const { data, isLoading, error } = useQuery({
    ...subListQuery(q, page, size),
    // The old page stays up while the next loads: a list that blanks on
    // every click reads as broken.
    placeholderData: keepPreviousData,
  });
  // The next two pages, fetched once this one has landed - after it, not
  // beside it, so the page on screen is never queued behind lookahead -
  // and not past the end. Click, and they are already there.
  const total = data?.total ?? 0;
  useEffect(() => {
    if (!data) return;
    for (let k = 1; k <= LOOKAHEAD; k++) {
      const p = page + k;
      if (p * size >= total) break;
      void qc.prefetchQuery(subListQuery(q, p, size));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, page, size, total]);

  const pageCount = Math.max(1, Math.ceil(total / size));
  const current = Math.min(page, pageCount - 1);
  const shown = data?.items ?? [];
  const { alarms, alerts } = counts;

  return (
    <td className="sub-cell" colSpan={span}>
      <div className="sub-wrap">
        {isLoading && <p className="muted small">Loading the conditions…</p>}
        {error && <p className="muted small">Could not load this room.</p>}
        {data && total === 0 && (
          <p className="muted small">
            {history
              ? 'Nothing has been raised here in this domain.'
              : 'Nothing open here in this domain.'}
          </p>
        )}
        {shown.length > 0 && (
          <>
            <div className="sub-caption">
              {alarms} alarm{alarms === 1 ? '' : 's'}
              {' · '}{alerts} alert{alerts === 1 ? '' : 's'}
              {history && <span className="muted">{' · '}open and closed</span>}
            </div>
            <table className="sub-table">
              <thead>
                <tr>
                  <th>Class</th><th>Severity</th><th>Condition</th>
                  <th>Device</th><th>Detail</th>
                  <th className="mid">Since</th><th className="mid">Last seen</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((a) => {
                  const alert = a.response_class === 'alert';
                  return (
                    <tr key={a.id} className="row-open"
                        onClick={() => onOpen(a)}
                        // A click handler on a <tr> is invisible to the
                        // accessibility tree and unreachable from the
                        // keyboard: the row is not focusable and Enter does
                        // nothing, so the detail simply does not exist for
                        // anyone not using a mouse.
                        role="button" tabIndex={0}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            onOpen(a);
                          }
                        }}
                        aria-label={`${humanise(a.alarm_type)} on `
                                    + `${a.device_name ?? 'the platform'}`}
                        title="Open the full condition">
                      <td>
                        <span className={`cls-pill ${alert ? 'alert' : 'alarm'}`}>
                          {alert ? 'ALERT' : 'ALARM'}
                        </span>
                      </td>
                      <td><StatusChip status={a.severity} /></td>
                      <td>
                        <span className="n">{humanise(a.alarm_type)}</span>
                        {a.instance && !isUuid(a.instance) && (
                          <span className="on-instance"> on {a.instance}</span>
                        )}
                      </td>
                      <td>
                        {a.device_id
                          ? <Link to={`/devices/${a.device_id}`}
                                  onClick={(e) => e.stopPropagation()}>
                              {a.device_name}
                            </Link>
                          : <span className="muted">platform</span>}
                        {a.rack_name && (
                          <span className="muted small"> · {a.rack_name}</span>
                        )}
                      </td>
                      {/* The words, with the reading folded in where the words
                          do not already carry it. A rule usually writes the
                          numbers into its own sentence, and a separate column
                          beside "93.0 C above 80.0 C" was the same fact twice. */}
                      <td className="muted">
                        {detail(a) ?? <span className="dash">&mdash;</span>}
                      </td>
                      <td className="mid muted">{relativeTime(a.first_seen)}</td>
                      {/* A closed row shows when it CLOSED, not when it was
                          last seen: those are the same instant for a cleared
                          condition, and "last seen" invites reading it as
                          still happening. */}
                      <td className="mid muted">
                        {a.state === 'CLEARED'
                          ? <span className="cleared-at">cleared {relativeTime(a.cleared_at ?? a.last_seen)}</span>
                          : relativeTime(a.last_seen)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {total > SUB_PAGE && (
              <Pagination page={current + 1} pageSize={size} shown={shown.length}
                          total={total} hasNext={current < pageCount - 1}
                          onPage={(p) => setPage(p - 1)}
                          onSize={(n) => { setSize(n); setPage(0); }} />
            )}
          </>
        )}
      </div>
    </td>
  );
}

type SortKey = 'room' | 'site' | 'qty' | 'alerts' | 'devices';


/** A facet row: the same population the rows total, split one way - and
 *  pressed, a filter on it.
 *
 *  The form is the segmented control every other filter in the product
 *  wears (the asset trends' ranges, the scope tabs): one strip, hairline-
 *  divided, pressed cells filled with the accent underline. An ALL cell
 *  leads it and is the pressed one when nothing else is - so the strip
 *  always shows a state, and clearing is a click on the cell that says so
 *  rather than a link somewhere else. Severity rides as a dot inside the
 *  cell, not as the cell's colour: the control's colour says "pressed",
 *  the dot says "critical", and neither has to be read from the other.
 *
 *  The counts are ALWAYS the unfiltered population, so a pressed cell can
 *  be compared with its neighbours and un-pressed; the rows below are the
 *  narrowed one. Cells in one strip combine as OR, the two strips as AND,
 *  and the server does the arithmetic for both. */
function FacetMenu({ id, label, entries, on, onToggle, onClear, tip, hue, open, onOpen }: {
  id: string; label: string; entries: { key: string; label: string; n: number }[];
  on: string[]; onToggle: (key: string) => void; onClear: () => void;
  /** What a value means, on hover - the taxonomy's own words. */
  tip?: (key: string) => string | undefined;
  /** Whether a value carries a severity dot. */
  hue?: boolean;
  open: boolean; onOpen: (open: boolean) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const isOn = (e: { key: string }) =>
    on.some((k) => k.toLowerCase() === e.key.toLowerCase());
  // A pressed value stays listed even when the other menus have narrowed
  // it to nothing: a filter the reader cannot see is one they cannot lift.
  const shown = entries.filter((e) => e.n > 0 || isOn(e));
  const pressed = shown.filter(isOn);
  const total = shown.reduce((t, e) => t + e.n, 0);

  // Outside click or Escape closes the open menu. Escape is caught on the
  // way down so the sheet's own Escape does not take the whole panel with it.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); onOpen(false); }
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey, true);
    };
  }, [open, onOpen]);

  if (!shown.length) return null;
  // What the button says: the values pressed, or All. Three or more are
  // counted rather than listed, or the bar would wrap on a bad day.
  const summary = pressed.length === 0 ? 'All'
    : pressed.length <= 2 ? pressed.map((e) => e.label).join(', ')
      : `${pressed.length} picked`;
  return (
    <div className={`facet-menu${pressed.length ? ' on' : ''}`} ref={ref}>
      <button type="button" aria-haspopup="true" aria-expanded={open}
              aria-controls={id} onClick={() => onOpen(!open)}>
        <span className="fm-label">{label}</span>
        <span className="fm-value">{summary}</span>
        <span className="fm-caret" aria-hidden />
      </button>
      {open && (
        <div className="facet-pop" id={id} role="group" aria-label={`${label} filter`}>
          <div className="fp-head">
            <span>{label}<b>{total.toLocaleString()}</b></span>
            {pressed.length > 0 && (
              <button type="button" className="fp-clear" onClick={onClear}>Clear</button>
            )}
          </div>
          <ul className="fp-list">
            {shown.map((e) => (
              <li key={e.key}>
                <label className={isOn(e) ? 'on' : ''} title={tip?.(e.key)}>
                  <input type="checkbox" checked={isOn(e)}
                         onChange={() => onToggle(e.key)} />
                  {hue && <i className={`sw ${e.key}`} aria-hidden />}
                  <span className="fp-name">{e.label}</span>
                  <b>{e.n.toLocaleString()}</b>
                </label>
              </li>
            ))}
          </ul>
        </div>
      )}
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

/** The devices figure that matches the columns beside it.
 *
 *  A panel listing alarms and alerts must count the devices behind both, or a
 *  row reads "0 devices" next to four alerts and the column looks broken. An
 *  alarms-only panel counts only the devices with something to answer - there,
 *  a device that is merely warm is not one of them. */
const deviceCount = (r: AlarmDrillRow, withAlerts: boolean) => (
  withAlerts ? (r.devices_all ?? r.devices) : r.devices);

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
    deviceCount(r, withAlerts), r.critical, r.major,
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
  const [tab, setTab] = useState<Tab>('live');
  const [pageSize, setPageSize] = useState(25);
  const [page, setPage] = useState(0);
  // Which rooms are open. A set, not one id: comparing two rooms is the
  // ordinary reason anybody expands anything.
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  // Which rooms have their trend showing - a second, independent expansion,
  // so a room's fortnight can sit under its conditions rather than replace
  // them.
  const [trendOpen, setTrendOpen] = useState<Set<string>>(() => new Set());
  // The whole panel's trend - the estate's, or the site's or room's the
  // panel was opened on. Same question as a row's trend button, asked of
  // the scope over the rows rather than of one of them.
  const [overallTrend, setOverallTrend] = useState(false);
  // The pressed chips. Reset with the tab, like every other expansion:
  // a filter chosen against the open population is not one the history
  // was asked for.
  const [facets, setFacets] = useState<Facets>(NO_FACETS);
  // Which filter menu is open, if any. One at a time.
  const [openFacet, setOpenFacet] = useState<keyof Facets | null>(null);
  // The condition somebody clicked. The panel already lists the estate's
  // alarms room by room, so the detail belongs on top of that view rather
  // than on a page that replaces it.
  const [openAlarm, setOpenAlarm] = useState<Alarm | null>(null);
  const filtering = facetsActive(facets);
  const history = tab === 'history';
  // The history window: one for the tab, read by the rows, the chips, the
  // lists under the rows and every trend chart. Presets end today; CUSTOM
  // is two dates. The live tab has no window - what is open is what is
  // open, whenever it started.
  const [preset, setPreset] = useState<Preset>('30D');
  const [since, setSince] = useState(() => {
    const d = new Date(); d.setDate(d.getDate() - 29); return isoDay(d);
  });
  const [until, setUntil] = useState(() => isoDay(new Date()));
  const custom = preset === CUSTOM;
  const spanDays = daysBetween(since, until);
  const windowOk = !custom || (spanDays >= 1 && spanDays <= MAX_DAYS);
  const win: TrendWindow | undefined = !history ? undefined
    : custom ? { since, until }
      : (() => {
          const w = WINDOWS.find((x) => x.label === preset) ?? WINDOWS[1];
          const d = new Date(); d.setDate(d.getDate() - (w.days - 1));
          return { since: isoDay(d), until: isoDay(new Date()) };
        })();
  const said = !history ? ''
    : custom ? `from ${shortDay(since)} to ${shortDay(until)}`
      : (WINDOWS.find((x) => x.label === preset) ?? WINDOWS[1]).said;
  // A window the server would refuse is not asked; the bar says why.
  const askable = !history || windowOk;
  const wkey = [win?.since ?? '', win?.until ?? ''];
  // The live rooms refresh while the panel is up, so the counts move and
  // each room's version moves with them - and only the rooms that changed
  // have their open lists fetched again. History is a closed population
  // and refreshes only when the window or a chip changes.
  const refresh = history ? false : 30_000;

  // Escape closes. A surface that covers the page and can only be dismissed
  // with the mouse traps a keyboard user.
  useEffect(() => {
    // Not while a maximized chart is up: that Escape is the modal's, and
    // taking the sheet down with it would drop two layers on one key.
    const onKey = (e: KeyboardEvent) => {
      // Nor while a condition is open on top, for the same reason.
      if (e.key === 'Escape' && !maxOpen() && !openAlarm) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, openAlarm]);

  // One query, however many categories the counter covers. Asking per category
  // and stitching the answers together in the browser gave one room two rows
  // and no honest way to count its devices - a device faulting in two domains
  // is one device, and only the database can say so.
  // The tab is part of the key: live and history are two populations of the
  // same rooms, and the server does the arithmetic for whichever is asked.
  // This one is UNFILTERED: what a row's chips count while nothing else
  // is pressed.
  const { data: whole, isLoading, error } = useQuery({
    queryKey: ['estate-alarms', tab, ...categories, ...wkey],
    queryFn: () => api.estateAlarms(categories, history ? 'all' : 'open', undefined, win),
    enabled: askable,
    refetchInterval: refresh,
    placeholderData: (prev) => prev,
  });
  // And the narrowed one, only while something is pressed: the rows, the
  // headline and the device counts come from here then.
  const { data: narrowed, isFetching: narrowing } = useQuery({
    queryKey: ['estate-alarms', tab, ...categories, ...wkey, ...facetKey(facets)],
    queryFn: () => api.estateAlarms(categories, history ? 'all' : 'open', facets, win),
    enabled: filtering && askable,
    refetchInterval: refresh,
    placeholderData: (prev) => prev,
  });
  const data = filtering ? narrowed : whole;

  // Each row's chips count every OTHER row's filter and never their own:
  // press Server and the conditions narrow to what servers raise, while
  // the Device row stays whole so Switch is still there to swap to. A row
  // with nothing pressed reads the narrowed response, which IS "everything
  // but this row". A pressed row needs its own question with its chips
  // left out - one query per pressed row, and none while only one row is
  // pressed, since "all but the only one" is the whole.
  const pressedRows = FACET_KEYS.filter((k) => facets[k].length > 0);
  const minusRows = pressedRows.length > 1 ? pressedRows : [];
  const minus = useQueries({
    queries: minusRows.map((row) => {
      const others: Facets = { ...facets, [row]: [] };
      return {
        queryKey: ['estate-alarms', tab, ...categories, ...wkey, ...facetKey(others)],
        queryFn: () => api.estateAlarms(categories, history ? 'all' : 'open', others, win),
        enabled: askable,
        refetchInterval: refresh,
        placeholderData: (prev: AlarmDrill | undefined) => prev,
      };
    }),
  });
  const sourceFor = (row: keyof Facets): AlarmDrill | undefined => {
    if (!filtering) return whole;
    if (!facets[row].length) return narrowed ?? whole;
    const i = minusRows.indexOf(row);
    return i >= 0 ? (minus[i].data ?? whole) : whole;
  };

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

  // The alert column belongs to a panel about a domain, not to one about what
  // has to be answered - there, every row would carry a number the panel is
  // deliberately not counting. Declared up here because the sort reads it: the
  // devices column sorts on the figure it prints, not on the other one.
  const withAlerts = !alarmsOnly;

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
          : sort === 'devices' ? deviceCount(x, withAlerts)
            : sort === 'alerts' ? x.alerts
              : x.qty);
    const l = pick(a); const r = pick(b);
    if (l === r) return a.room_name.localeCompare(b.room_name);
    return (l < r ? -1 : 1) * dir;
  });

  const pageCount = Math.max(1, Math.ceil(sorted.length / pageSize));
  const current = Math.min(page, pageCount - 1);
  const rows = sorted.slice(current * pageSize, current * pageSize + pageSize);

  // Facet counts are scoped like the rows, and come from whichever
  // response answers "everything but this row" (see sourceFor).
  const rowsOf = (d?: AlarmDrill): AlarmDrillRow[] => (d?.rows ?? [])
    .filter(inScope)
    .filter((r) => !alarmsOnly || r.qty > 0);
  const rowsFor = (row: keyof Facets) => rowsOf(sourceFor(row));
  const sevRows = rowsFor('severity');
  const severity = SEVERITIES.map((k) => ({
    key: k,
    label: k.toUpperCase(),
    n: sevRows.reduce((n, r) => n + (r.by_severity?.[k] ?? 0), 0),
  }));
  const detRows = rowsFor('detection');
  const detections = (taxonomy?.detections ?? []).map((d) => ({
    key: d.key as AlarmDetection,
    label: d.label,
    n: detRows.reduce((n, r) => n + (r.by_detection?.[d.key] ?? 0), 0),
  }));
  const detectionTip = (key: string) =>
    taxonomy?.detections.find((d) => d.key === key)?.description;
  // What KIND of thing is alarming. No fixed vocabulary - an IT panel is
  // servers and switches, a power panel UPSs and PDUs - so the chips are
  // whatever the rows hold, worst first, plus whatever is pressed. Labelled
  // from the whole response, which carries every kind once.
  const kindLabel = (key: string) =>
    whole?.by_device_type.find((k) => k.key === key)?.label
      ?? key.replace(/_/g, ' ').replace(/^./, (ch) => ch.toUpperCase());
  const kindSrc = sourceFor('device_type');
  const kindRows = rowsOf(kindSrc);
  const kindKeys = [...new Set([
    ...(kindSrc?.by_device_type ?? []).map((k) => k.key), ...facets.device_type])];
  const kinds = kindKeys.map((key) => ({
    key,
    label: kindLabel(key),
    n: kindRows.reduce((n, r) => n + (r.by_device_type?.[key] ?? 0), 0),
    devices: kindRows.reduce((n, r) => n + (r.devices_by_type?.[key] ?? 0), 0),
  })).sort((a, b) => b.n - a.n || a.label.localeCompare(b.label));
  const kindTip = (key: string) => {
    const k = kinds.find((x) => x.key === key);
    if (!k) return undefined;
    const what = k.devices === 1 ? k.label.toLowerCase() : `${k.label.toLowerCase()}s`;
    return `${k.n.toLocaleString()} alarm${k.n === 1 ? '' : 's'} on `
      + `${k.devices.toLocaleString()} ${what}`;
  };
  // The condition itself. Open vocabulary like the kinds; labelled from the
  // taxonomy the panel already holds, which is where the legend's words are.
  const conditionLabel = (key: string) =>
    taxonomy?.conditions.find((c) => c.key === key)?.label
      ?? key.replace(/_/g, ' ').replace(/^./, (ch) => ch.toUpperCase());
  const condSrc = sourceFor('alarm_type');
  const condRows = rowsOf(condSrc);
  const condKeys = [...new Set([
    ...(condSrc?.by_condition ?? []).map((c) => c.key), ...facets.alarm_type])];
  const conditions = condKeys.map((key) => ({
    key,
    label: conditionLabel(key),
    n: condRows.reduce((n, r) => n + (r.by_condition?.[key] ?? 0), 0),
    // Summed over the scoped rooms, like the count beside it: a device
    // stands in one room, so the room figures add without double counting.
    devices: condRows.reduce((n, r) => n + (r.devices_by_condition?.[key] ?? 0), 0),
  })).sort((a, b) => b.n - a.n || a.label.localeCompare(b.label));
  const conditionTip = (key: string) => {
    const c = conditions.find((x) => x.key === key);
    const detail = taxonomy?.conditions.find((x) => x.key === key)?.detail;
    if (!c) return detail;
    const head = `${c.n.toLocaleString()} alarm${c.n === 1 ? '' : 's'} on `
      + `${c.devices.toLocaleString()} device${c.devices === 1 ? '' : 's'}`;
    return detail ? `${head}. ${detail}` : head;
  };
  // Alarm or alert: the two classes the row columns already keep apart.
  // Not on an alarms-only panel, where there is nothing to choose.
  const classRows = rowsFor('response_class');
  const classes = (taxonomy?.response_classes ?? []).map((c) => ({
    key: c.key,
    label: c.label,
    n: classRows.reduce((n, r) => n + (c.key === 'alarm' ? r.qty : r.alerts), 0),
  }));
  const classTip = (key: string) =>
    taxonomy?.response_classes.find((c) => c.key === key)?.description;
  // Where it stands in an incident, and whether anybody has it. Fixed
  // vocabularies; the counts are the rows' own.
  const standRows = rowsFor('standing');
  const standings = STANDINGS.map((s) => ({
    ...s,
    n: standRows.reduce((n, r) =>
      n + (r.by_standing?.[s.key as keyof AlarmDrillRow['by_standing']] ?? 0), 0),
  }));
  const stateRows = rowsFor('state');
  const states = STATES.map((s) => ({
    ...s,
    n: stateRows.reduce((n, r) =>
      n + (r.by_state?.[s.key as keyof AlarmDrillRow['by_state']] ?? 0), 0),
  }));
  const fixedTip = (list: { key: string; tip: string }[]) => (key: string) =>
    list.find((x) => x.key === key)?.tip;
  // One menu per axis. Severity keys travel upper-cased, as the API spells
  // them; everything else as the response keyed it.
  const menu = (row: keyof Facets, label: string,
                entries: { key: string; label: string; n: number }[],
                tip?: (key: string) => string | undefined, hue?: boolean) => (
    <FacetMenu id={`facet-${row}`} label={label} entries={entries} hue={hue}
               on={facets[row]}
               onToggle={(k) => toggleFacet(row, row === 'severity' ? k.toUpperCase() : k)}
               onClear={() => clearFacet(row)}
               tip={tip}
               open={openFacet === row}
               onOpen={(o) => setOpenFacet(o ? row : null)} />
  );

  function toggleFacet(row: keyof Facets, key: string) {
    setFacets((f) => {
      const cur = f[row];
      const next = cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key];
      return { ...f, [row]: next };
    });
    setPage(0);
  }
  function clearFacet(row: keyof Facets) {
    setFacets((f) => ({ ...f, [row]: [] }));
    setPage(0);
  }

  // What the headline is "of" while a filter is pressed: the same figure it
  // would show with nothing pressed, so the two numbers are the same kind.
  const wholeTotal = !whole ? 0
    : scope
      ? rowsOf(whole).reduce((n, r) => n + r.qty + (alarmsOnly ? 0 : r.alerts), 0)
      : (alarmsOnly ? whole.alarms : whole.total);

  // Every category at once. Concatenating seven definitions produced a
  // paragraph nobody would read and pushed the table below the fold; one
  // sentence says the same thing.
  const everything = categories.length > 2;
  const definitions = (taxonomy?.categories ?? []).filter(
    (c) => categories.includes(c.key));
  const where = scope ? `in ${scope.label}` : 'on the estate';
  const blurb = history
    ? `Everything ${where} raised in this domain ${said}, open and `
      + 'cleared alike, grouped by the room it is in. Open a room '
      + 'for its conditions; the trend button shows what it raised over '
      + 'time.'
    : everything
      ? `Every open alarm ${where}, `
        + 'grouped by the room it is in and the kind of thing that is wrong. '
        + 'Every one of them, whatever it is a consequence of.'
      : definitions.map((c) => c.description).join(' ');

  function toggle(roomId: string) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (!next.delete(roomId)) next.add(roomId);
      return next;
    });
  }

  // The first page, fetched when the pointer reaches the button that would
  // open the room. The hand arrives a couple of hundred milliseconds before
  // the click, which is about what the page takes, so the room opens full
  // rather than on "Loading the conditions…". Keyed on the row's version
  // like the list itself, so a room already fetched at this version is
  // not fetched again.
  const qc = useQueryClient();
  const prime = (r: AlarmDrillRow) => {
    void qc.prefetchQuery(subListQuery(
      { roomId: r.room_id, categories, tab, facets, window: win, version: r.changed_at },
      0, SUB_PAGE));
  };

  function toggleTrend(roomId: string) {
    setTrendOpen((prev) => {
      const next = new Set(prev);
      if (!next.delete(roomId)) next.add(roomId);
      return next;
    });
  }

  // Switching populations closes every expansion: an open row was answering
  // the other tab's question, and a stale sub-table under a fresh headline is
  // exactly the disagreement this panel exists to avoid.
  function switchTab(next: Tab) {
    if (next === tab) return;
    setTab(next);
    setOpen(new Set());
    setTrendOpen(new Set());
    setOverallTrend(false);
    setFacets(NO_FACETS);
    setOpenFacet(null);
    setPage(0);
  }

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
    const site = scope ? `-${scope.label}` : '';
    a.download = `${title}${site}-alarms-${tab}`
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
              {/* Filtered, the headline says what it is a share of; the
                  strips below say which share. */}
              {data && filtering && (
                <span className="of-which filtered">
                  of {wholeTotal.toLocaleString()} · filtered
                  {narrowing ? '…' : ''}
                </span>
              )}
              {data && !filtering && !alarmsOnly && (
                <span className="of-which">
                  {history
                    ? `${alarms} raised as alarm${alarms === 1 ? '' : 's'}`
                    : alarms > 0
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
          {error && <Notice tone="error">Could not load the rooms behind this counter.</Notice>}
          {isLoading && <p className="muted">Loading…</p>}

          {/* Two populations of the same rooms: what is open now, and
              everything that has been raised. The trend lives inside the
              history, per room, where "is this normal for here" is asked. */}
          <div className="panel-tabs" role="tablist" aria-label="Panel view">
            <button role="tab" aria-selected={tab === 'live'}
                    className={tab === 'live' ? 'active' : ''}
                    onClick={() => switchTab('live')}>Live</button>
            <button role="tab" aria-selected={tab === 'history'}
                    className={tab === 'history' ? 'active' : ''}
                    onClick={() => switchTab('history')}>History</button>
            {history && (
              <button className={`trend-toggle ${overallTrend ? 'on' : ''}`}
                      onClick={() => setOverallTrend((v) => !v)}
                      aria-pressed={overallTrend}
                      title="Raised over time, across every room below">
                <TrendGlyph />
                <span>Trend</span>
              </button>
            )}
          </div>

          {history && overallTrend && (
            <div className="panel-trend">
              <AlarmTrend categories={categories} scope={scope} window={win}
                          facets={filtering ? facets : undefined} />
            </div>
          )}

          <>

          {whole && (
            /* One line: a menu per axis with what is pressed written on its
               button, Clear all once anything is, and the search at the
               end. Seven rows of chips were half the sheet, and the table
               is what the sheet is for. Standing is the live fold and means
               nothing in history, where a cleared consequence has already
               dropped its mark; Class has nothing to choose on an
               alarms-only panel. */
            <div className="facet-bar">
              {history && (
                <div className="facet-window">
                  <Seg value={preset} label="How far back"
                       options={[...WINDOWS.map((w) => ({ key: w.label as Preset, label: w.label })),
                                 { key: CUSTOM as Preset, label: CUSTOM }]}
                       onChange={(k) => { setPreset(k); setPage(0); }} />
                  {custom && (
                    <>
                      <label>From <input type="date" value={since} max={until}
                                         onChange={(e) => { setSince(e.target.value); setPage(0); }} /></label>
                      <label>To <input type="date" value={until} min={since}
                                       max={isoDay(new Date())}
                                       onChange={(e) => { setUntil(e.target.value); setPage(0); }} /></label>
                      {!windowOk && (
                        <span className="why">
                          {spanDays < 1 ? 'The end is before the start.' : `At most ${MAX_DAYS} days.`}
                        </span>
                      )}
                    </>
                  )}
                </div>
              )}
              <span className="facet-label">Filter</span>
              {menu('severity', 'Severity', severity, (k) => `Only ${k} conditions`, true)}
              {!alarmsOnly && menu('response_class', 'Class', classes, classTip)}
              {menu('state', 'Handled', states, fixedTip(STATES))}
              {!history && menu('standing', 'Standing', standings, fixedTip(STANDINGS))}
              {menu('detection', 'Found by', detections, detectionTip)}
              {menu('device_type', 'Device', kinds, kindTip)}
              {menu('alarm_type', 'Condition', conditions, conditionTip)}
              {filtering && (
                <button type="button" className="facet-clear-all"
                        onClick={() => { setFacets(NO_FACETS); setPage(0); }}>
                  Clear all
                </button>
              )}
              <label className="search">
                <span className="glass" aria-hidden />
                <input value={search} placeholder="SEARCH"
                       aria-label="Search rooms and sites"
                       onChange={(e) => { setSearch(e.target.value); setPage(0); }} />
              </label>
            </div>
          )}

          {data && sorted.length === 0 && (
            q ? (
              <p className="muted">No room matches “{search}”.</p>
            ) : (
              // Nothing more than the sentence. The cleared conditions used to
              // be listed here as the empty state; they now have a tab of
              // their own, and a live panel that showed history when quiet
              // was two tabs saying one thing.
              <p className="muted">
                {history
                  ? `Nothing has been raised in ${title.toLowerCase()}`
                  : `Nothing open in ${title.toLowerCase()}`}
                {scope ? ` in ${scope.label}` : ''}{history ? ` ${said}` : ''}.
                {!history && ' Cleared conditions are under History.'}
              </p>
            )
          )}

          {sorted.length > 0 && (
            <div className="estate-scroll">
              <table className="estate-table">
                <thead>
                  <tr>
                    <th className="twist-col" aria-label="Expand" />
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
                    {history && <th className="mid">Trend</th>}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => {
                    const isOpen = open.has(r.room_id);
                    const trendOn = history && trendOpen.has(r.room_id);
                    // One narrower than it was: the ENTER column went with
                    // the page it opened.
                    const span = (withAlerts ? 9 : 8) + (history ? 1 : 0);
                    return (
                      <Fragment key={r.room_id}>
                        <tr className={(r.critical ? 'lead-critical' : 'lead-warn')
                                       + (isOpen ? ' expanded' : '')}>
                      <td className="twist-col">
                        <button className={`twist ${isOpen ? 'on' : ''}`}
                                onClick={() => toggle(r.room_id)}
                                onMouseEnter={() => { if (!isOpen) prime(r); }}
                                onFocus={() => { if (!isOpen) prime(r); }}
                                aria-expanded={isOpen}
                                aria-label={`${isOpen ? 'Hide' : 'Show'} the`
                                            + ` conditions in ${r.room_name}`}>
                          <span aria-hidden>▸</span>
                        </button>
                      </td>
                      <td>
                        {/* The name opens the row as well. A 20px chevron is a
                            small target for something the row is entirely
                            about. */}
                        <button className="name-cell as-link"
                                onClick={() => toggle(r.room_id)}
                                onMouseEnter={() => { if (!isOpen) prime(r); }}
                                onFocus={() => { if (!isOpen) prime(r); }}>
                          <span className="n">{r.room_name}</span>
                        </button>
                      </td>
                      <td className="muted">{r.site_code}</td>
                      <td className="mid muted">{r.floor ?? '—'}</td>
                      <td className="num"><span className="qty">{r.qty}</span></td>
                      {/* Muted on purpose. It is here to say what else is
                          going on in this room, not to compete with the
                          number somebody is acting on. */}
                      {withAlerts && (
                        <td className="num muted">
                          <Tip tip={<><b>{r.alerts}</b> informational condition
                            {r.alerts === 1 ? '' : 's'} here — nothing that
                            needs a response tonight</>}>
                            {r.alerts || <span className="dash">—</span>}
                          </Tip>
                        </td>
                      )}
                      <td className="num">
                        <Tip tip={withAlerts
                          ? 'Distinct devices with anything open here — one device faulting twice is one device'
                          : 'Distinct devices with an alarm here'}>
                          {deviceCount(r, withAlerts)}
                        </Tip>
                      </td>
                      <td className="num">{r.critical || <span className="dash">—</span>}</td>
                      <td className="num">{r.major || <span className="dash">—</span>}</td>
                      {history && (
                        <td className="mid">
                          <button className={`trend-btn ${trendOn ? 'on' : ''}`}
                                  onClick={() => toggleTrend(r.room_id)}
                                  aria-pressed={trendOn}
                                  title="Raised over time in this room"
                                  aria-label={`${trendOn ? 'Hide' : 'Show'} the`
                                              + ` trend for ${r.room_name}`}>
                            <TrendGlyph />
                          </button>
                        </td>
                      )}
                        </tr>
                        {/* The trend sits directly under its row, above the
                            conditions: the button that opens it is on the row,
                            and a chart that appears below nine thousand
                            expanded conditions is a chart nobody finds. */}
                        {trendOn && (
                          <tr className="sub-row">
                            <td className="sub-cell" colSpan={span}>
                              <div className="sub-wrap">
                                <AlarmTrend categories={categories}
                                            scope={{ kind: 'room', id: r.room_id,
                                                     label: r.room_name }}
                                            window={win}
                                            facets={filtering ? facets : undefined} />
                              </div>
                            </td>
                          </tr>
                        )}
                        {isOpen && (
                          <tr className="sub-row">
                            <RoomConditions roomId={r.room_id} categories={categories}
                                            span={span} tab={tab} facets={facets}
                                            window={win} version={r.changed_at}
                                            counts={{ alarms: r.qty, alerts: r.alerts }}
                                            onOpen={setOpenAlarm} />
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {sorted.length > 0 && (
            <div className="sheet-foot">
              {/* The shared pager; the search's narrowing is said in the noun
                  so "of 3 rooms (of 16)" still tells the reader a filter is
                  on. */}
              <Pagination page={current + 1} pageSize={pageSize} shown={rows.length}
                          total={sorted.length} hasNext={current < pageCount - 1}
                          noun={sorted.length !== all.length
                            ? `rooms (of ${all.length})` : 'rooms'}
                          onPage={(p) => setPage(p - 1)}
                          onSize={(n) => { setPageSize(n); setPage(0); }} />
              <button className="row-btn csv" onClick={download}>DOWNLOAD CSV</button>
            </div>
          )}

          {unlocated > 0 && (
            <p className="muted small" style={{ marginTop: 14 }}>
              Not counted above: {unlocated} condition{unlocated === 1 ? '' : 's'}
              {' '}in the monitoring itself. {unlocated === 1 ? 'It hangs' : 'They hang'}
              {' '}off the pipeline rather than off a device on a floor, so
              {unlocated === 1 ? ' it has' : ' they have'} no room, no site and no
              row here — the MONITORING badge carries
              {unlocated === 1 ? ' it' : ' them'}.{' '}
              <Link to="/platform">Platform health →</Link>
            </p>
          )}
          </>
        </div>
      </section>
      {openAlarm && (
        <AlarmDetailModal alarm={openAlarm}
                          onClose={() => setOpenAlarm(null)}
                          onOpenAlarm={(id) => { api.alarm(id).then(setOpenAlarm).catch(() => undefined); }} />
      )}
    </div>
  );
}
