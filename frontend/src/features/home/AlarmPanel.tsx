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

import { Fragment, useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type Alarm,
  type AlarmCategory,
  type AlarmDetection,
  type AlarmDrillRow,
  type AlarmTaxonomy,
} from '../../api/client';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { StatusChip } from '../../components/StatusChip';
import { Tip, useHoverTip } from '../../components/HoverTip';
import { metaFor } from '../../components/alertMeta';
import { humanise, relativeTime } from '../../lib/format';

const SEVERITIES = ['critical', 'major', 'minor', 'warning'] as const;

// Worst first, so an opened room reads its own headline on the first line.
const SEV_RANK: Record<string, number> = {
  CRITICAL: 0, MAJOR: 1, MINOR: 2, WARNING: 3, INFO: 4,
};

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
/** Which part of the lifecycle the room list is showing.
 *
 *  `open` is the default and matches the counter the panel was opened from -
 *  a drill-down that disagreed with the number above it would be worse than no
 *  drill-down. The other two answer a different question: what has this room
 *  been doing, and did the thing I fixed actually clear. That history is
 *  already in the table; it was simply unreachable from here.
 */
type Lifecycle = 'open' | 'cleared' | 'all';

const LIFECYCLE: { key: Lifecycle; label: string; states?: string[] }[] = [
  { key: 'open', label: 'Active' },
  { key: 'cleared', label: 'Cleared', states: ['CLEARED'] },
  { key: 'all', label: 'All', states: ['ACTIVE', 'ACKNOWLEDGED', 'CLEARED'] },
];

function RoomConditions({ roomId, categories, span }: {
  roomId: string; categories: AlarmCategory[]; span: number;
}) {
  const [view, setView] = useState<Lifecycle>('open');
  const states = LIFECYCLE.find((l) => l.key === view)?.states;
  const { data, isLoading, error } = useQuery({
    queryKey: ['room-conditions', roomId, view, ...categories],
    queryFn: () => api.roomConditions(roomId, categories, states),
    staleTime: 15_000,
  });

  const items = [...(data?.items ?? [])].sort((a, b) => {
    // History reads newest-first: once a condition is closed its severity is
    // no longer a call to action, and "when" is the only ordering that helps.
    if (view === 'cleared') {
      return (b.cleared_at ?? b.last_seen).localeCompare(a.cleared_at ?? a.last_seen);
    }
    const cls = (x: Alarm) => (x.response_class === 'alert' ? 1 : 0);
    if (cls(a) !== cls(b)) return cls(a) - cls(b);
    const ra = SEV_RANK[a.severity] ?? 9;
    const rb = SEV_RANK[b.severity] ?? 9;
    if (ra !== rb) return ra - rb;
    return b.last_seen.localeCompare(a.last_seen);
  });

  const alarms = items.filter((a) => a.response_class !== 'alert').length;
  const alerts = items.length - alarms;

  return (
    <td className="sub-cell" colSpan={span}>
      <div className="sub-wrap">
        {/* Outside the items check on purpose: "no active faults" and "no
            history" are different answers, and the reader can only tell which
            one they are looking at if the switch is still there. */}
        <div className="lifecycle" role="group" aria-label="Which conditions to show">
          {LIFECYCLE.map((l) => (
            <button key={l.key}
                    className={`sort ${view === l.key ? 'on' : ''}`}
                    aria-pressed={view === l.key}
                    onClick={() => setView(l.key)}>
              {l.label}
            </button>
          ))}
        </div>
        {isLoading && <p className="muted small">Loading the conditions…</p>}
        {error && <p className="muted small">Could not load this room.</p>}
        {data && !items.length && (
          <p className="muted small">
            {view === 'cleared'
              ? 'Nothing has cleared here in this domain.'
              : 'Nothing open here in this domain.'}
          </p>
        )}
        {items.length > 0 && (
          <>
            <div className="sub-caption">
              {alarms} alarm{alarms === 1 ? '' : 's'}
              {' · '}{alerts} alert{alerts === 1 ? '' : 's'}
              {view !== 'open' && <span className="muted">{' · '}
                {view === 'cleared' ? 'closed' : 'open and closed'}</span>}
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
                {items.map((a) => {
                  const alert = a.response_class === 'alert';
                  return (
                    <tr key={a.id}>
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
                          ? <Link to={`/devices/${a.device_id}`}>{a.device_name}</Link>
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
          </>
        )}
      </div>
    </td>
  );
}

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

/** Conditions raised per day over the last fortnight, in this panel's
 *  scope. The table below says what is happening NOW; this says whether now
 *  is normal - a hall raising six a day for two weeks and a hall that just
 *  started are different problems wearing the same count. Built from the
 *  same rows the history shows (roots only, open and cleared), bucketed on
 *  first_seen: raised-per-day survives the clears that empty the table. */
function AlarmTrend({ categories, scope }: {
  categories: AlarmCategory[]; scope?: PanelScope;
}) {
  const { bind, tipEl } = useHoverTip();
  const { data } = useQuery({
    queryKey: ['alarm-trend', scope?.id ?? '', ...categories],
    queryFn: () => api.alarms({
      state: ['ACTIVE', 'ACKNOWLEDGED', 'CLEARED'],
      category: categories,
      room: scope?.kind === 'room' ? scope.id : undefined,
      limit: '500',
    }),
    staleTime: 60_000,
  });

  const items = (data?.items ?? [])
    .filter((a) => scope?.kind !== 'site' || a.datacenter_code === scope.label);

  const days: string[] = [];
  for (let i = 13; i >= 0; i--) {
    const d = new Date();
    d.setDate(d.getDate() - i);
    days.push(d.toISOString().slice(0, 10));
  }
  const counts = new Map(days.map((d) => [d, 0]));
  for (const a of items) {
    const day = a.first_seen.slice(0, 10);
    if (counts.has(day)) counts.set(day, (counts.get(day) ?? 0) + 1);
  }
  const max = Math.max(1, ...counts.values());
  const total = [...counts.values()].reduce((t, n) => t + n, 0);

  if (!data) return <p className="muted">Loading the trend…</p>;
  return (
    <>
      <p className="muted">
        {total} condition{total === 1 ? '' : 's'} raised in the last 14 days
        {scope ? ` in ${scope.label}` : ''}.
      </p>
      <div className="alarm-trend" role="img"
           aria-label="Conditions raised per day, last 14 days">
        {days.map((day) => {
          const n = counts.get(day) ?? 0;
          return (
            <div className="col" key={day}
                 {...bind(<><b>{day.slice(5)}</b> {n} raised</>)}>
              <div className="barwrap">
                <div className="v">{n || ''}</div>
                <div className="bar" style={{ height: `${(n / max) * 100}%` }} />
              </div>
              <div className="k">{day.slice(5)}</div>
            </div>
          );
        })}
        {tipEl}
      </div>
    </>
  );
}

/** The empty state that still answers a question. With nothing open in
 *  scope, the panel shows what CLEARED there recently instead - a quiet
 *  counter is exactly when somebody asks what happened overnight, and this
 *  is why the counters stay clickable at zero. */
function RecentCleared({ categories, scope, title }: {
  categories: AlarmCategory[]; scope?: PanelScope; title: string;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ['recent-cleared', scope?.id ?? '', ...categories],
    queryFn: () => api.alarms({
      state: ['CLEARED'],
      category: categories,
      room: scope?.kind === 'room' ? scope.id : undefined,
      limit: '100',
    }),
    staleTime: 30_000,
  });

  const items = (data?.items ?? [])
    // The alarms endpoint scopes by room but not by site; the rows carry
    // their site, so a site scope narrows here.
    .filter((a) => scope?.kind !== 'site' || a.datacenter_code === scope.label)
    .sort((a, b) => (b.cleared_at ?? b.last_seen)
      .localeCompare(a.cleared_at ?? a.last_seen))
    .slice(0, 15);

  return (
    <>
      <p className="muted">
        Nothing open in {title.toLowerCase()}{scope ? ` in ${scope.label}` : ''}.
      </p>
      {isLoading && <p className="muted small">Looking at the history…</p>}
      {!isLoading && items.length === 0 && (
        <p className="muted small">Nothing has cleared here either.</p>
      )}
      {items.length > 0 && (
        <>
          <p className="sub-caption" style={{ marginTop: 14 }}>
            Recently cleared
          </p>
          <div className="estate-scroll">
            <table className="estate-table">
              <thead>
                <tr>
                  <th>Cleared</th><th>Device</th><th>Condition</th>
                  <th>Where</th>
                </tr>
              </thead>
              <tbody>
                {items.map((a) => (
                  <tr key={a.id}>
                    <td className="muted">
                      {relativeTime(a.cleared_at ?? a.last_seen)}
                    </td>
                    <td>
                      <Link to={`/devices/${a.device_id}`}>{a.device_name}</Link>
                    </td>
                    <td>{humanise(a.alarm_type)}</td>
                    <td className="muted">
                      {[a.datacenter_code, a.room_name].filter(Boolean).join(' · ') || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}

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
  const [tab, setTab] = useState<'list' | 'trend'>('list');
  const [pageSize, setPageSize] = useState(25);
  const [page, setPage] = useState(0);
  // Which rooms are open. A set, not one id: comparing two rooms is the
  // ordinary reason anybody expands anything.
  const [open, setOpen] = useState<Set<string>>(() => new Set());

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

  function toggle(roomId: string) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (!next.delete(roomId)) next.add(roomId);
      return next;
    });
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

          {/* Two answers to two questions: WHAT is happening (the rooms and
              their conditions, or the history when quiet) and WHETHER now is
              normal (the fortnight trend, full-size). */}
          <div className="panel-tabs" role="tablist" aria-label="Panel view">
            <button role="tab" aria-selected={tab === 'list'}
                    className={tab === 'list' ? 'active' : ''}
                    onClick={() => setTab('list')}>Conditions</button>
            <button role="tab" aria-selected={tab === 'trend'}
                    className={tab === 'trend' ? 'active' : ''}
                    onClick={() => setTab('trend')}>Trend</button>
          </div>

          {tab === 'trend' && (
            <AlarmTrend categories={categories} scope={scope} />
          )}
          {tab === 'list' && (
          <>

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
            q ? (
              <p className="muted">No room matches “{search}”.</p>
            ) : (
              <RecentCleared categories={categories} scope={scope}
                             title={title} />
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
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => {
                    const isOpen = open.has(r.room_id);
                    return (
                      <Fragment key={r.room_id}>
                        <tr className={(r.critical ? 'lead-critical' : 'lead-warn')
                                       + (isOpen ? ' expanded' : '')}>
                      <td className="twist-col">
                        <button className={`twist ${isOpen ? 'on' : ''}`}
                                onClick={() => toggle(r.room_id)}
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
                                onClick={() => toggle(r.room_id)}>
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
                      <td className="num">
                        <Link className="row-btn" to={`/alarms?room=${r.room_id}`}
                              style={{ display: 'inline-block', lineHeight: '26px',
                                       textAlign: 'center' }}>
                          ENTER
                        </Link>
                      </td>
                        </tr>
                        {isOpen && (
                          <tr className="sub-row">
                            <RoomConditions roomId={r.room_id} categories={categories}
                                            span={withAlerts ? 10 : 9} />
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
          )}
        </div>
      </section>
    </div>
  );
}
