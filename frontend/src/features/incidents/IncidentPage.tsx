import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import {
  api, type AnomalyIncidentAlarm, type AnomalyIncidentDetail, type Series,
} from '../../api/client';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { ActionGlyph } from '../../components/ActionGlyph';
import { Notice } from '../../components/Notice';
import { TimeChart, type ChartMark } from '../../components/TimeChart';
import { usePaged } from '../../components/Pagination';
import { AlarmDetailModal } from '../alarms/AlarmDetail';
import { CoolingTrace } from '../devices/CoolingTrace';
import { humanise, relativeTime } from '../../lib/format';
import '../devices/devices.css';
import './incidents.css';

/** One incident, read the way a handover reads it: what caused it, what it
 *  reached, is it over. Those three stand above the fold as sections, with
 *  the timeline beside them as the spine. The heavy lists - every alarm,
 *  the findings, the record - are tabs below, because they are reference,
 *  not the story. The alarm modal stays what it is: one device saying one
 *  thing; it opens here with ?alarm= so the reader never leaves the page. */

const TABS = [
  { id: 'alarms', label: 'Alarms' },
  { id: 'findings', label: 'Findings' },
  { id: 'record', label: 'Record' },
] as const;
type Tab = typeof TABS[number]['id'];
const isTab = (s: string | null): s is Tab => TABS.some((t) => t.id === s);

const SEV_RANK: Record<string, number> = { CRITICAL: 0, MAJOR: 1, MINOR: 2, WARNING: 3, INFO: 4 };
const sevClass = (s?: string | null) => {
  const w = (s ?? '').toLowerCase();
  return w === 'critical' || w === 'major' ? w : w === 'minor' || w === 'warning' ? 'warn' : '';
};
const sevWord = (s?: string | null) => (s ?? 'info').toLowerCase();

/** The cause's domain in the words the home rail uses. */
const CAUSE: Record<string, { glyph: 'cooling' | 'power'; word: string }> = {
  power: { glyph: 'power', word: 'power' },
  plant: { glyph: 'cooling', word: 'cooling plant' },
  loop: { glyph: 'cooling', word: 'cooling loop' },
  unit: { glyph: 'cooling', word: 'cooling unit' },
  zone: { glyph: 'cooling', word: 'airflow' },
  server: { glyph: 'cooling', word: 'equipment' },
  room: { glyph: 'cooling', word: 'airflow' },
};

/** The one reading that says whether the root is doing its job, by class.
 *  A chiller is its chilled-water supply, a CRAH its supply air, a UPS its
 *  load, a PDU its current. Unknown classes fall back to intake air. */
function rootMetrics(deviceType: string | null | undefined): string[] {
  const t = (deviceType ?? '').toLowerCase();
  if (t.includes('chiller') || t.includes('cdu')) return ['water_supply_temp', 'water_return_temp'];
  if (t.includes('crah') || t.includes('crac') || t.includes('ahu')) return ['supply_air_temp', 'return_air_temp'];
  if (t.includes('ups')) return ['load_pct'];
  if (t.includes('pdu') || t.includes('rpp') || t.includes('panel')) return ['current'];
  if (t.includes('pump')) return ['water_flow'];
  return ['inlet_temperature', 'ambient_temperature'];
}

const BUCKET_MS: Record<string, number> = {
  raw: 60_000, '1m': 60_000, '5m': 300_000, '1h': 3600_000, '1d': 86400_000,
};
const ms = (iso: string | null | undefined) => (iso ? new Date(iso).getTime() : NaN);

function duration(fromMs: number, toMs: number): string {
  const s = Math.max(0, Math.round((toMs - fromMs) / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ${m - h * 60} min`;
  const d = Math.floor(h / 24);
  return `${d} d ${h - d * 24} h`;
}

function clock(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}
function dayOf(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { day: '2-digit', month: 'short' });
}
function stamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  return `${dayOf(iso)} ${clock(iso)}`;
}

// ------------------------------------------------------------------ page

export function IncidentPage() {
  const { id = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const tab: Tab = isTab(params.get('tab')) ? (params.get('tab') as Tab) : 'alarms';
  const goTab = (t: Tab) => {
    const next = new URLSearchParams(params);
    if (t === 'alarms') next.delete('tab'); else next.set('tab', t);
    setParams(next, { replace: true });
  };

  const q = useQuery<AnomalyIncidentDetail>({
    queryKey: ['anomaly-incident', id],
    queryFn: () => api.anomalyIncident(id),
    enabled: Boolean(id),
    refetchInterval: (query) => (query.state.data?.state === 'open' ? 30_000 : false),
    retry: false,
  });

  // The alarm modal, in place: ?alarm=<id> opens it over this page, and
  // its own "cause" / "explains" links swap the id rather than navigating.
  const linkedAlarmId = params.get('alarm');
  const linkedAlarm = useQuery({
    queryKey: ['alarm', linkedAlarmId],
    queryFn: () => api.alarm(linkedAlarmId!),
    enabled: Boolean(linkedAlarmId),
  });
  const openAlarm = (alarmId: string) =>
    setParams((p) => { p.set('alarm', alarmId); return p; }, { replace: true });
  const closeAlarm = () =>
    setParams((p) => { p.delete('alarm'); return p; }, { replace: true });

  if (q.isLoading) return <IncidentSkeleton />;
  if (q.isError) {
    const gone = String(q.error).includes('404');
    return (
      <div className="dv-page">
        <Notice tone="error" heading={gone ? 'This incident is no longer on record' : 'The incident could not be read'}>
          {gone ? 'It may have been superseded by a merge, or the link is old.' : String(q.error)}
          {' '}<Link to="/">Back to the home page</Link>
        </Notice>
      </div>
    );
  }
  if (!q.data) return null;
  const inc = q.data;
  const open = inc.state === 'open';
  const worst = inc.evidence.worst ?? (open ? 'INFO' : null);
  const cause = CAUSE[inc.tier] ?? CAUSE.unit;
  const root = inc.alarms.find((a) => a.role === 'root') ?? null;
  const peers = inc.alarms.filter((a) => a.role === 'peer');
  const rooms = [...(inc.evidence.by_room ?? [])]
    .sort((a, b) => (SEV_RANK[a.worst] ?? 9) - (SEV_RANK[b.worst] ?? 9) || b.alarms - a.alarms);
  const devices = new Set(inc.alarms.map((a) => a.device_id)).size;
  const folded = inc.evidence.folded ?? inc.alarms.filter((a) => a.role === 'folded').length;

  return (
    <div className="dv-page ip-page">
      <nav className="dv-crumb" aria-label="Where this is">
        <Link to="/">Home</Link>
        {inc.site_code && (<><span aria-hidden>›</span><span title={inc.site_name ?? undefined}>{inc.site_code}</span></>)}
        {inc.room_id && (<><span aria-hidden>›</span><Link to={`/?room=${inc.room_id}`}>{inc.room_name}</Link></>)}
        <span aria-hidden>›</span><span>Incident</span>
      </nav>

      <header className={`dv-head${open && worst ? ` sev-${sevWord(worst)}` : ''}`}>
        <div className="dv-title">
          <h2>
            {inc.title}
            <span className={`chip ip-state ${open ? 'is-open' : 'is-done'}`}>
              <span className="dot" aria-hidden />{open ? 'Open' : 'Resolved'}
            </span>
          </h2>
          <p className="subtitle">
            {worst && <span className={`ip-sev ${sevClass(worst)}`}>{sevWord(worst)}</span>}
            {worst ? ' · ' : ''}{cause.word}
            {' · opened '}{stamp(inc.opened_at)}
            {open
              ? <> · last activity {relativeTime(inc.last_activity_at)}</>
              : <> · resolved {stamp(inc.resolved_at)}</>}
          </p>
          <div className="dv-tools">
            {inc.root_alarm_id && (
              <button type="button" className="dv-act" onClick={() => openAlarm(inc.root_alarm_id!)}>
                <ActionGlyph kind="open" />Open root alarm
              </button>
            )}
            {inc.root_device_id && (
              <Link className="dv-act" to={`/devices/${inc.root_device_id}`}>
                <ActionGlyph kind="open" />Root device
              </Link>
            )}
            {inc.room_id && (
              <Link className="dv-act" to={`/floorplan?room=${inc.room_id}${inc.root_device_id ? `&device=${inc.root_device_id}` : ''}`}>
                <ActionGlyph kind="cube" />Room in 3D
              </Link>
            )}
            <CopyLink />
          </div>
        </div>
        <Band inc={inc} open={open} devices={devices} rooms={rooms.length} folded={folded} peers={peers.length} />
      </header>

      <div className="ip-grid">
        <div className="dv-col">
          <CauseCard inc={inc} root={root} peers={peers} folded={folded} onAlarm={openAlarm} />
          <ReachCard inc={inc} rooms={rooms} cause={cause.glyph} />
          <EvidenceCard inc={inc} root={root} />
        </div>
        <Timeline inc={inc} root={root} onAlarm={openAlarm} />
      </div>

      <div className="dv-tabs" role="tablist" aria-label="Incident lists">
        {TABS.map((t) => (
          <button key={t.id} type="button" role="tab" id={`ip-tab-${t.id}`}
                  aria-selected={tab === t.id} aria-controls="ip-panel" onClick={() => goTab(t.id)}>
            {t.label}
            {t.id === 'alarms' && <span className={`n${open && worst ? ` sev-${sevWord(worst)}` : ''}`}>{inc.alarms.length}</span>}
            {t.id === 'findings' && inc.findings.length > 0 && <span className="n">{inc.findings.length}</span>}
          </button>
        ))}
      </div>
      <div className="dv-tab" id="ip-panel" role="tabpanel" aria-labelledby={`ip-tab-${tab}`}>
        {tab === 'alarms' && <AlarmTable inc={inc} rooms={rooms} onAlarm={openAlarm} />}
        {tab === 'findings' && <FindingTable inc={inc} />}
        {tab === 'record' && <RecordCard inc={inc} />}
      </div>

      {linkedAlarmId && linkedAlarm.data && (
        <AlarmDetailModal alarm={linkedAlarm.data} onClose={closeAlarm} onOpenAlarm={openAlarm} />
      )}
      {linkedAlarmId && linkedAlarm.isError && (
        <Notice tone="warn">That alarm could not be found; the link may be old.</Notice>
      )}
    </div>
  );
}

// ------------------------------------------------------------- skeleton

/** The page at its working height before the record arrives: head, three
 *  cards, the rail. Nothing jumps when the words land. */
function IncidentSkeleton() {
  return (
    <div className="dv-page ip-page" aria-busy>
      <nav className="dv-crumb"><span className="dv-skel">Home › Incident</span></nav>
      <header className="dv-head">
        <div className="dv-title">
          <h2><span className="dv-skel">An incident title of the usual length</span></h2>
          <p className="subtitle"><span className="dv-skel">critical · cooling plant · opened</span></p>
        </div>
        <div className="dv-band">
          {['Duration', 'Alarms', 'Devices', 'Rooms'].map((k) => (
            <div key={k} className="dv-tile no-glyph"><b className="dv-skel">00</b><span>{k}</span></div>
          ))}
        </div>
      </header>
      <div className="ip-grid">
        <div className="dv-col">
          {['Cause', 'Reach', 'Evidence'].map((k) => (
            <section key={k} className="dv-card ip-skel-card"><h3>{k}</h3>
              <p className="dv-skel">The cause of the incident in one line</p>
              <p className="dv-skel">A second line about it</p>
            </section>
          ))}
        </div>
        <aside className="dv-card ip-rail"><h3>Timeline</h3>
          {[0, 1, 2, 3, 4].map((n) => <p key={n} className="dv-skel">08:02:10 a device did a thing</p>)}
        </aside>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------- band

function Band({ inc, open, devices, rooms, folded, peers }: {
  inc: AnomalyIncidentDetail; open: boolean; devices: number; rooms: number; folded: number; peers: number;
}) {
  // The duration ticks while the incident is open; a resolved one is a fact.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!open) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [open]);
  const end = open ? now : ms(inc.resolved_at);
  const worst = sevClass(inc.evidence.worst);
  return (
    <div className="dv-band">
      <div className={`dv-tile no-glyph${open && worst ? ` tone-${worst}` : ''}`}>
        <b>{duration(ms(inc.opened_at), end)}</b>
        <span>{open ? 'open for' : 'lasted'}</span>
      </div>
      <div className="dv-tile no-glyph">
        <b>{inc.alarms.length}</b>
        <span>{folded > 0 ? `alarms · ${folded} folded` : 'alarms'}</span>
      </div>
      <div className="dv-tile no-glyph"><b>{devices}</b><span>devices</span></div>
      <div className="dv-tile no-glyph"><b>{rooms}</b><span>{rooms === 1 ? 'room' : 'rooms'}</span></div>
      {peers > 0 && (
        <div className="dv-tile no-glyph"><b>{peers}</b><span>{peers === 1 ? 'peer unit' : 'peer units'}</span></div>
      )}
      {inc.findings.length > 0 && (
        <div className="dv-tile no-glyph"><b>{inc.findings.length}</b><span>findings</span></div>
      )}
    </div>
  );
}

function CopyLink() {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href.split('?')[0]);
      setDone(true);
      window.setTimeout(() => setDone(false), 2000);
    } catch { /* clipboard refused: the address bar still has it */ }
  };
  return (
    <button type="button" className="dv-act" onClick={copy} aria-live="polite">
      <ActionGlyph kind="tag" />{done ? 'Link copied' : 'Copy link'}
    </button>
  );
}

// ---------------------------------------------------------------- cause

function CauseCard({ inc, root, peers, folded, onAlarm }: {
  inc: AnomalyIncidentDetail; root: AnomalyIncidentAlarm | null; peers: AnomalyIncidentAlarm[];
  folded: number; onAlarm: (id: string) => void;
}) {
  const ev = inc.evidence;
  const others = (ev.other_root_alarms as string[] | undefined) ?? [];
  const lead = root && peers.length
    ? Math.max(...peers.map((p) => ms(p.first_seen))) - ms(root.first_seen) : null;
  const why: string[] = [];
  if (root) why.push(`${humanise(root.tier ?? inc.tier)} tier: the earliest open root in this scope`);
  if (peers.length) why.push(`${peers.length} more ${peers.length === 1 ? 'unit' : 'units'} stopped within ${duration(0, lead ?? 0)} of it: ${peers.map((p) => p.device_name).join(', ')}`);
  if (folded > 0) why.push(`${folded} ${folded === 1 ? 'alarm' : 'alarms'} downstream were folded under it, so the console shows one line, not ${folded + 1}`);
  if (ev.air_side === 'room') why.push('the heat is on the room’s air side, which this unit serves');

  if (inc.tier === 'room' || !root) {
    return (
      <section className="dv-card ip-cause">
        <h3>Cause<span className="lead">none named</span></h3>
        <p className="ip-lede">{inc.summary ?? 'Heat with no cooling alarm behind it. The room is warm and nothing has said why.'}</p>
      </section>
    );
  }

  return (
    <section className="dv-card ip-cause">
      <h3>Cause</h3>
      <div className="ip-root">
        <Link to={`/devices/${root.device_id}`} className="ip-root-name">{root.device_name}</Link>
        <span className="muted"> {humanise(root.device_type)}{inc.room_name ? ` · ${inc.room_name}` : ''}</span>
      </div>
      <button type="button" className={`ip-alarm-line sev-${sevWord(root.severity)}`} onClick={() => onAlarm(root.id)}
              title="Open this alarm">
        <span className="sev">{sevWord(root.severity)}</span>
        <span className="type">{humanise(root.alarm_type)}{root.instance ? ` · ${root.instance}` : ''}</span>
        <span className="when">since {stamp(root.first_seen)}{root.cleared_at ? ` · cleared ${clock(root.cleared_at)}` : ' · still open'}</span>
      </button>
      {root.message && <p className="ip-quote">{root.message}</p>}

      {why.length > 0 && (
        <>
          <h4 className="ip-h4">Why this root</h4>
          <ul className="ip-why">{why.map((w) => <li key={w}>{w}</li>)}</ul>
        </>
      )}

      {peers.length > 0 && (
        <>
          <h4 className="ip-h4">Down with it</h4>
          <ul className="ip-peers">
            {peers.map((p) => (
              <li key={p.id}>
                <Link to={`/devices/${p.device_id}`}>{p.device_name}</Link>
                <button type="button" className="linklike" onClick={() => onAlarm(p.id)}>
                  {humanise(p.alarm_type)}
                </button>
                <span className="muted">{clock(p.first_seen)}</span>
              </li>
            ))}
          </ul>
          <p className="ip-hint">Look for what they share first: feeder, controls, chilled water.</p>
        </>
      )}

      {others.length > 0 && (
        <details className="ip-fold">
          <summary>{others.length} other {others.length === 1 ? 'candidate' : 'candidates'} for the root</summary>
          <ul className="ip-peers">
            {others.map((aid) => {
              const a = inc.alarms.find((x) => x.id === aid);
              return (
                <li key={aid}>
                  {a ? <Link to={`/devices/${a.device_id}`}>{a.device_name}</Link> : <span className="muted">alarm</span>}
                  <button type="button" className="linklike" onClick={() => onAlarm(aid)}>
                    {a ? humanise(a.alarm_type) : aid.slice(0, 8)}
                  </button>
                  {a && <span className="muted">{clock(a.first_seen)}</span>}
                </li>
              );
            })}
          </ul>
        </details>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- reach

/** The one drawn thing on the page: the root at the left, a branch to each
 *  room it reached, worst first. Each branch is the room's own response in
 *  numbers, and opens that room's alarms. */
function ReachCard({ inc, rooms, cause }: {
  inc: AnomalyIncidentDetail;
  rooms: NonNullable<AnomalyIncidentDetail['evidence']['by_room']>;
  cause: 'cooling' | 'power';
}) {
  const tiers = (inc.evidence.members_by_tier as Record<string, number> | undefined) ?? {};
  const tierRows = Object.entries(tiers).sort((a, b) => b[1] - a[1]);
  const rootName = inc.root_device_name ?? inc.room_name ?? 'cause';
  return (
    <section className="dv-card ip-reach">
      <h3>Reach<span className="lead">{rooms.length === 0 ? 'no room beyond the cause' : `${rooms.length} ${rooms.length === 1 ? 'room' : 'rooms'}`}</span></h3>
      {rooms.length > 0 ? (
        <div className="ip-map">
          <div className="ip-map-root">
            <span className={`ind on ${cause === 'power' ? 'pwr' : 'cool'} sev-${sevWord(inc.evidence.worst)}`} aria-hidden>
              <CategoryGlyph kind={cause} />
            </span>
            <span className="ip-map-root-name">{rootName}</span>
          </div>
          <ul className="ip-branches" aria-label={`Rooms ${rootName} reached`}>
            {rooms.map((r, i) => (
              <li key={r.room_id ?? `none-${i}`} className={i === rooms.length - 1 ? 'is-last' : ''}>
                <span className="ip-branch" aria-hidden />
                <Link className="ip-room" to={r.room_id ? `/?room=${r.room_id}` : '#'}
                      aria-disabled={!r.room_id} title={r.room_id ? 'This room’s alarms' : undefined}>
                  <span className="ip-room-name">{r.room_name ?? 'No room'}</span>
                  <span className="ip-room-n">{r.devices} {r.devices === 1 ? 'device' : 'devices'} · {r.alarms} {r.alarms === 1 ? 'alarm' : 'alarms'}</span>
                  <span className={`ip-room-sev ${sevClass(r.worst)}`}>{sevWord(r.worst)}</span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="dv-empty">Every alarm in this incident stands on the cause itself.</p>
      )}
      {tierRows.length > 0 && (
        <p className="ip-tiers">
          <span className="k">By tier</span>
          {tierRows.map(([t, n]) => <span key={t} className="ip-tier">{humanise(t)} <b>{n}</b></span>)}
        </p>
      )}
    </section>
  );
}

// ------------------------------------------------------------- evidence

/** Two pictures: the root's own reading across the incident, with its
 *  alarms as bars under the axis, and the intake air of one hot device per
 *  room it reached. The window runs from an hour before the root to a
 *  quarter hour after the resolution, or to now. */
function EvidenceCard({ inc, root }: { inc: AnomalyIncidentDetail; root: AnomalyIncidentAlarm | null }) {
  const [openTrace, setOpenTrace] = useState(false);
  const start = ms(inc.opened_at) - 3600_000;
  const end = inc.resolved_at ? ms(inc.resolved_at) + 900_000 : Date.now();
  const startIso = new Date(start).toISOString();
  const endIso = new Date(end).toISOString();

  const rootMetricsList = rootMetrics(root?.device_type);
  const rootHist = useQuery({
    queryKey: ['incident-root-history', inc.id, root?.device_id, startIso.slice(0, 16), endIso.slice(0, 16)],
    queryFn: () => api.history(root!.device_id, rootMetricsList, startIso, endIso),
    enabled: Boolean(root),
    retry: false,
  });

  // One hot device per reached room: the earliest inlet / temperature
  // alarm in it. Up to four rooms, four lines.
  const hot = useMemo(() => {
    const rooms = inc.evidence.by_room ?? [];
    const picks: { room: string; device_id: string; device_name: string }[] = [];
    for (const r of rooms) {
      const a = inc.alarms
        .filter((x) => x.role !== 'root' && x.role !== 'peer' && /temp|inlet|hot|heat/i.test(x.alarm_type))
        .sort((x, y) => x.first_seen.localeCompare(y.first_seen))
        .find((x) => !picks.some((p) => p.device_id === x.device_id));
      if (a) picks.push({ room: r.room_name ?? 'room', device_id: a.device_id, device_name: a.device_name });
      if (picks.length >= 4) break;
    }
    return picks;
  }, [inc]);
  const hotHist = useQuery({
    queryKey: ['incident-hot-history', inc.id, hot.map((h) => h.device_id).join(','), startIso.slice(0, 16), endIso.slice(0, 16)],
    queryFn: async () => Promise.all(hot.map(async (h) => ({
      ...h, out: await api.history(h.device_id, ['inlet_temperature'], startIso, endIso),
    }))),
    enabled: hot.length > 0,
    retry: false,
  });

  const rootMarks: ChartMark[] = root ? inc.alarms
    .filter((a) => a.device_id === root.device_id)
    .map((a) => ({ start: ms(a.first_seen), end: a.cleared_at ? ms(a.cleared_at) : null,
                   severity: a.severity, label: humanise(a.alarm_type) })) : [];
  const rootSeries = (rootHist.data?.series ?? []).filter((s) => s.points.length > 0);
  const hotSeries: Series[] = (hotHist.data ?? []).flatMap((h) =>
    h.out.series.filter((s) => s.points.length > 0).map((s) => ({ ...s, instance: `${h.room} · ${h.device_name}` })));
  const coolingRoot = root && /chiller|crah|crac|cdu|ahu|pump|cooling/i.test(root.device_type);

  return (
    <section className="dv-card ip-evidence">
      <h3>Evidence<span className="lead">{dayOf(startIso)} {clock(startIso).slice(0, 5)} to {inc.resolved_at ? clock(endIso).slice(0, 5) : 'now'}</span></h3>
      {root && (
        <div className="ip-chart">
          <p className="ip-chart-cap">{root.device_name}: {rootSeries[0] ? humanise(rootSeries[0].metric) : humanise(rootMetricsList[0])}</p>
          {rootHist.isLoading && <p className="dv-empty">Reading…</p>}
          {!rootHist.isLoading && rootSeries.length === 0 && (
            <p className="dv-empty">No {humanise(rootMetricsList[0]).toLowerCase()} history for this window.</p>
          )}
          {rootSeries.length > 0 && rootHist.data && (
            <TimeChart series={rootSeries} unit={rootSeries[0].unit}
                       bucketMs={BUCKET_MS[rootHist.data.interval] ?? 60_000}
                       marks={rootMarks} window={[start, end]} />
          )}
        </div>
      )}
      {hot.length > 0 && (
        <div className="ip-chart">
          <p className="ip-chart-cap">Intake air, one hot device per room reached</p>
          {hotHist.isLoading && <p className="dv-empty">Reading…</p>}
          {!hotHist.isLoading && hotSeries.length === 0 && <p className="dv-empty">No intake history for this window.</p>}
          {hotSeries.length > 0 && (
            <TimeChart series={hotSeries} unit={hotSeries[0].unit}
                       bucketMs={BUCKET_MS[hotHist.data?.[0]?.out.interval ?? '1m'] ?? 60_000}
                       marks={root ? [{ start: ms(root.first_seen), end: root.cleared_at ? ms(root.cleared_at) : null,
                                        severity: root.severity, label: `${root.device_name} ${humanise(root.alarm_type)}` }] : []}
                       window={[start, end]} />
          )}
        </div>
      )}
      {!root && hot.length === 0 && <p className="dv-empty">Nothing to draw: no root device and no hot device named.</p>}
      {coolingRoot && (
        <details className="ip-fold" open={openTrace} onToggle={(e) => setOpenTrace((e.target as HTMLDetailsElement).open)}>
          <summary>Cooling chain behind {root!.device_name}</summary>
          {openTrace && <CoolingTrace deviceId={root!.device_id} />}
        </details>
      )}
    </section>
  );
}

// ------------------------------------------------------------- timeline

type Ev = { t: number; kind: 'root' | 'peer' | 'member' | 'folded' | 'finding' | 'clear' | 'resolved';
            title: string; sub?: string; severity?: string; alarmId?: string; deviceId?: string; n?: number };

/** The spine. Every alarm as it was raised, in role, clears as they came,
 *  findings between them, the resolution at the end. Folded consequences
 *  collapse to one row at the time the first of them was raised, because
 *  two hundred symptoms say one thing. The lead between root and the first
 *  thing after it is labelled: that gap is the proof the root is right. */
function Timeline({ inc, root, onAlarm }: {
  inc: AnomalyIncidentDetail; root: AnomalyIncidentAlarm | null; onAlarm: (id: string) => void;
}) {
  const [showFolded, setShowFolded] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const events = useMemo(() => {
    const out: Ev[] = [];
    const foldedRows: Ev[] = [];
    for (const a of inc.alarms) {
      const e: Ev = { t: ms(a.first_seen), kind: a.role, title: a.device_name,
                      sub: humanise(a.alarm_type), severity: a.severity, alarmId: a.id, deviceId: a.device_id };
      if (a.role === 'folded') foldedRows.push(e); else out.push(e);
      if (a.cleared_at && a.role !== 'folded') {
        out.push({ t: ms(a.cleared_at), kind: 'clear', title: a.device_name, sub: `${humanise(a.alarm_type)} cleared`, alarmId: a.id });
      }
    }
    if (foldedRows.length) {
      if (showFolded) out.push(...foldedRows);
      else out.push({ t: Math.min(...foldedRows.map((f) => f.t)), kind: 'folded',
                      title: `${foldedRows.length} folded ${foldedRows.length === 1 ? 'consequence' : 'consequences'}`,
                      sub: 'explained by the cause; not shown on the console', n: foldedRows.length });
    }
    for (const f of inc.findings) {
      out.push({ t: ms(f.first_seen), kind: 'finding', title: f.device_name, sub: `${f.detector_id} · ${f.summary}` });
    }
    if (inc.resolved_at) out.push({ t: ms(inc.resolved_at), kind: 'resolved', title: 'Resolved', sub: `after ${duration(ms(inc.opened_at), ms(inc.resolved_at))}` });
    return out.sort((a, b) => a.t - b.t || (a.kind === 'root' ? -1 : 0));
  }, [inc, showFolded]);

  const LIMIT = 40;
  const rows = showAll ? events : events.slice(0, LIMIT);
  const rootT = root ? ms(root.first_seen) : NaN;
  const firstAfter = root ? events.find((e) => e.t > rootT && e.kind !== 'clear' && e.kind !== 'resolved') : undefined;
  const foldedTotal = inc.alarms.filter((a) => a.role === 'folded').length;

  let lastDay = '';
  return (
    <aside className="dv-card ip-rail" aria-label="Timeline">
      <h3>Timeline<span className="lead">{events.length} events</span></h3>
      <ol className="ip-tl">
        {rows.map((e, i) => {
          const day = dayOf(new Date(e.t).toISOString());
          const dayRow = day !== lastDay; lastDay = day;
          const leadHere = firstAfter && e === firstAfter;
          return (
            <li key={`${e.kind}-${e.alarmId ?? e.title}-${e.t}-${i}`} className={`ip-ev is-${e.kind}${e.severity ? ` sev-${sevWord(e.severity)}` : ''}`}>
              {dayRow && <span className="ip-day">{day}</span>}
              {leadHere && <span className="ip-lead">lead {duration(rootT, e.t)}</span>}
              <span className="ip-ev-t">{clock(new Date(e.t).toISOString())}</span>
              <span className="ip-ev-dot" aria-hidden />
              <span className="ip-ev-body">
                {e.kind === 'folded' ? (
                  <button type="button" className="linklike ip-ev-title" aria-expanded={showFolded}
                          onClick={() => setShowFolded(true)}>{e.title}</button>
                ) : e.alarmId ? (
                  <button type="button" className="linklike ip-ev-title" onClick={() => onAlarm(e.alarmId!)}>{e.title}</button>
                ) : (
                  <span className="ip-ev-title">{e.title}</span>
                )}
                {e.sub && <span className="ip-ev-sub">{e.sub}</span>}
                <span className="ip-ev-role">{e.kind === 'clear' ? 'cleared' : e.kind === 'resolved' ? '' : e.kind}{e.severity && e.kind !== 'clear' ? ` · ${sevWord(e.severity)}` : ''}</span>
              </span>
            </li>
          );
        })}
      </ol>
      <div className="ip-tl-foot">
        {events.length > LIMIT && !showAll && (
          <button type="button" className="linklike" onClick={() => setShowAll(true)}>Show all {events.length}</button>
        )}
        {showFolded && foldedTotal > 0 && (
          <button type="button" className="linklike" onClick={() => setShowFolded(false)}>Fold the {foldedTotal} consequences</button>
        )}
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------- tables

function AlarmTable({ inc, rooms, onAlarm }: {
  inc: AnomalyIncidentDetail;
  rooms: NonNullable<AnomalyIncidentDetail['evidence']['by_room']>;
  onAlarm: (id: string) => void;
}) {
  const [role, setRole] = useState('');
  const [sev, setSev] = useState('');
  const [text, setText] = useState('');
  const items = useMemo(() => inc.alarms
    .filter((a) => !role || a.role === role)
    .filter((a) => !sev || a.severity === sev)
    .filter((a) => !text || a.device_name.toLowerCase().includes(text.toLowerCase()) || a.alarm_type.toLowerCase().includes(text.toLowerCase()))
    .sort((a, b) => (SEV_RANK[a.severity] ?? 9) - (SEV_RANK[b.severity] ?? 9) || a.first_seen.localeCompare(b.first_seen)),
  [inc, role, sev, text]);
  const { rows, foot } = usePaged(items, { noun: 'alarms' });
  const sevs = [...new Set(inc.alarms.map((a) => a.severity))].sort((a, b) => (SEV_RANK[a] ?? 9) - (SEV_RANK[b] ?? 9));
  void rooms;
  return (
    <section>
      <h3>Alarms in this incident<span className="lead">{items.length} of {inc.alarms.length}</span></h3>
      <div className="ip-filters">
        <label>Role
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="">any</option>
            {['root', 'peer', 'member', 'folded'].map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <label>Severity
          <select value={sev} onChange={(e) => setSev(e.target.value)}>
            <option value="">any</option>
            {sevs.map((s) => <option key={s} value={s}>{s.toLowerCase()}</option>)}
          </select>
        </label>
        <label className="ip-search">Device or alarm
          <input type="search" value={text} onChange={(e) => setText(e.target.value)} placeholder="CRAH-05" />
        </label>
      </div>
      {items.length === 0 ? <p className="dv-empty">No alarm matches.</p> : (
        <div className="table-frame">
          <table className="ip-table">
            <thead>
              <tr><th>Device</th><th>Alarm</th><th>Severity</th><th>State</th><th>Role</th><th>Raised</th><th>Cleared</th></tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.id} className={a.cleared_at ? 'is-cleared' : ''}>
                  <td><Link to={`/devices/${a.device_id}`}>{a.device_name}</Link><span className="muted"> {humanise(a.device_type)}</span></td>
                  <td><button type="button" className="linklike" onClick={() => onAlarm(a.id)}>{humanise(a.alarm_type)}</button>{a.instance && <span className="muted"> · {a.instance}</span>}</td>
                  <td><span className={`chip ${sevClass(a.severity)}`}><span className="dot" aria-hidden />{sevWord(a.severity)}</span></td>
                  <td>{a.state.toLowerCase()}</td>
                  <td><span className={`ip-role is-${a.role}`}>{a.role}</span></td>
                  <td className="num">{stamp(a.first_seen)}</td>
                  <td className="num">{a.cleared_at ? stamp(a.cleared_at) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {foot}
    </section>
  );
}

function FindingTable({ inc }: { inc: AnomalyIncidentDetail }) {
  if (inc.findings.length === 0) {
    return (
      <section>
        <h3>Findings</h3>
        <p className="dv-empty">No detector flagged anything beyond the alarms.</p>
      </section>
    );
  }
  return (
    <section>
      <h3>Anomaly findings on the same chain<span className="lead">{inc.findings.length}</span></h3>
      <div className="table-frame">
        <table className="ip-table">
          <thead><tr><th>Detector</th><th>Device</th><th>What it saw</th><th>State</th><th>First</th><th>Last</th></tr></thead>
          <tbody>
            {inc.findings.map((f) => (
              <tr key={f.id}>
                <td className="num">{f.detector_id}</td>
                <td><Link to={`/devices/${f.device_id}`}>{f.device_name}</Link></td>
                <td>{f.summary}</td>
                <td>{f.state}</td>
                <td className="num">{stamp(f.first_seen)}</td>
                <td className="num">{stamp(f.last_seen)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function RecordCard({ inc }: { inc: AnomalyIncidentDetail }) {
  const ev = inc.evidence;
  const facts: [string, string][] = [
    ['Incident id', inc.id],
    ['Identity key', inc.root_key],
    ['Tier', inc.tier],
    ['State', inc.state],
    ['Opened', inc.opened_at],
    ['Last activity', inc.last_activity_at],
    ['Resolved', inc.resolved_at ?? '—'],
    ['Members on record', String(inc.member_count)],
    ['Alarm ids held', String(inc.alarm_count)],
    ['Correlator', ev.v_l5 != null ? `L5 v${String(ev.v_l5)}` : '—'],
    ['Evidence kind', String(ev.kind ?? '—')],
    ['Root alarm id', inc.root_alarm_id ?? '—'],
    ['Root device id', inc.root_device_id ?? '—'],
    ['Datacenter id', inc.datacenter_id ?? '—'],
    ['Room id', inc.room_id ?? '—'],
  ];
  return (
    <section>
      <h3>Record<span className="lead">as the correlator keeps it</span></h3>
      {inc.summary && <p className="ip-lede">{inc.summary}</p>}
      <dl className="dv-facts">
        {facts.map(([k, v]) => (
          <div key={k} className="dv-fact"><dt className="k">{k}</dt><dd className="v mono">{v}</dd></div>
        ))}
      </dl>
    </section>
  );
}
