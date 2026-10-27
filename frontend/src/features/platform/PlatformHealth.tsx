import { useMemo } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api, atLeast, type CollectorHealth, type CollectorsPage, type PlatformAlarmRow, type PlatformState,
} from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { humanise, relativeTime } from '../../lib/format';
import { useSocketStatus } from '../../ws/useSocket';
import './platform.css';
import { Notice } from '../../components/Notice';

/** Is the monitoring working?
 *
 *  Every other page answers a question about the datacenter, and every one
 *  of them shows the same thing when the collector dies as when the
 *  datacenter is quiet: no alarms, flat charts, nothing moving. This page is
 *  the one that can tell those apart.
 *
 *  It is laid out as the pipeline the readings travel: collectors, the
 *  queue, the ingest workers, the database, this browser. A stage that is
 *  not keeping up breaks the line, so where the silence comes from is read
 *  off the picture rather than worked out from four unrelated numbers. Below
 *  it, what is wrong now and what to do about it; the collectors one by one;
 *  and what closed in the last day, because a quiet open list is not the
 *  same as a quiet day. */

type Tone = 'ok' | 'warn' | 'critical' | 'unknown';

const SEVERITY_RANK: Record<string, number> = { CRITICAL: 3, MAJOR: 2, WARNING: 1 };

/** What to do first. Lifted from the operations runbook (docs/17 §4) and the
 *  rules' own reasons, one sentence each, so the page says the next step
 *  rather than only the finding. */
const WHAT_TO_CHECK: Record<string, string> = {
  collector_stale: 'Check the collector process on its host, then its log for NOAUTH or a missing DCIM_REDIS_URL. Its endpoints are not being polled; the devices themselves are probably fine.',
  collector_degraded: 'Drops are data that no longer exists anywhere. Check Redis is up, then the collector log for "shedding telemetry". If Redis is healthy, the collector is producing faster than it can publish.',
  ingest_lag_high: 'Usually a backlog draining after an outage: if rows are still climbing, wait. If they are not, check that the ingest workers are alive and Postgres is accepting connections.',
  ingest_stalled: 'No telemetry for three poll intervals. Nothing is being measured, so the absence of device alarms says nothing about the estate.',
  ingest_worker_stale: 'At WARNING, no heartbeat but data still arrives (an older worker build). At CRITICAL nothing arrives either: restart the ingest workers.',
  assignment_stale: 'The collector is polling a plan over five minutes old. It should pick up a new one within 30 s; if it does not, it cannot reach the API.',
  mapping_mismatch: 'The collector\'s point mappings differ from the platform\'s bundle, so it may be polling the wrong OIDs. Roll the collector forward.',
  db_pool_exhausted: 'Every database connection is busy and requests are queueing. Look for a long-running query or a transaction left open.',
  integration_credential_expiring: 'Rotate the integration\'s credential before it lapses, under Settings › Integrations.',
  integration_webhook_expiring: 'Re-register the webhook before it lapses, under Settings › Integrations.',
  integration_degraded: 'Outbound messages have been given up on. Check the integration\'s outbox and whether the remote service is answering.',
  pool_below_min_members: 'The pool has fewer healthy collectors than it requires. Bring a member back, or lower the minimum if the design changed.',
  collector_outdated: 'The collector runs a release too far behind the platform. Roll it out under Settings › Releases.',
  collector_capacity_high: 'The collector is shedding polls or near its budget. Add a collector to the pool or move endpoints off it.',
  pool_rate_budget_exceeded: 'The pool is asked for more polls per second than its members can sustain. Add a member or relax the poll profiles.',
  collector_misconfigured: 'The collector rejected its configuration. Open it under Settings › Collectors and read the error.',
  collector_duplicate: 'Two processes are running under one collector identity and alternating heartbeats. Stop the extra one.',
};

const trim0 = (s: string) => s.replace(/\.0$/, '');
function secs(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  if (v < 1) return `${(v * 1000).toFixed(0)} ms`;
  if (v < 90) return `${trim0(v.toFixed(1))} s`;
  if (v < 5400) return `${trim0((v / 60).toFixed(1))} min`;
  return `${trim0((v / 3600).toFixed(1))} h`;
}

/** A duration in the words an incident report uses. */
function span(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 90) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}

const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

function worstOf(rows: { severity: string }[]): string {
  return rows.reduce((w, r) => (SEVERITY_RANK[r.severity] ?? 0) > (SEVERITY_RANK[w] ?? 0) ? r.severity : w, 'WARNING');
}

const toneOfSeverity = (sev: string | null | undefined): Tone =>
  sev === 'CRITICAL' ? 'critical' : sev === 'MAJOR' || sev === 'WARNING' ? 'warn' : 'ok';

// ------------------------------------------------------------------ pipeline

interface Stage { key: string; name: string; tone: Tone; value: string; detail: string; to?: string }

function Pipeline({ stages }: { stages: Stage[] }) {
  return (
    <ol className="ph-pipe" aria-label="The telemetry pipeline, stage by stage">
      {stages.map((s, i) => (
        <li key={s.key} className={`ph-stage tone-${s.tone}`}>
          {i > 0 && <span className={`ph-link tone-${stages[i - 1].tone}`} aria-hidden />}
          <div className="ph-stage-body">
            <span className="ph-stage-name">
              <span className="ph-dot" aria-hidden />{s.to ? <Link to={s.to}>{s.name}</Link> : s.name}
            </span>
            <span className="ph-stage-value">{s.value}</span>
            <span className="ph-stage-detail">{s.detail}</span>
          </div>
        </li>
      ))}
    </ol>
  );
}

// ---------------------------------------------------------------- conditions

interface Cond {
  key: string; alarm_type: string; instance: string; severity: string;
  response_class: string; message: string;
  /** Persisted row, when the worker has raised it. */
  row?: PlatformAlarmRow;
  /** open: raised and still found · detected: found, not yet raised (dwell) ·
   *  clearing: raised, no longer found, waiting for the worker to close it. */
  life: 'open' | 'detected' | 'clearing';
}

function mergeConditions(h: CollectorHealth): Cond[] {
  const byKey = new Map<string, Cond>();
  for (const a of h.open_alarms) {
    byKey.set(`${a.alarm_type}|${a.instance}`, {
      key: `${a.alarm_type}|${a.instance}`, alarm_type: a.alarm_type, instance: a.instance,
      severity: a.severity, response_class: a.response_class ?? (SEVERITY_RANK[a.severity] >= 2 ? 'alarm' : 'alert'),
      message: a.message, row: a, life: 'clearing',
    });
  }
  for (const f of h.findings) {
    const k = `${f.alarm_type}|${f.instance}`;
    const held = byKey.get(k);
    if (held) { held.life = 'open'; held.severity = f.severity; held.message = f.message; continue; }
    byKey.set(k, {
      key: k, alarm_type: f.alarm_type, instance: f.instance, severity: f.severity,
      response_class: SEVERITY_RANK[f.severity] >= 2 ? 'alarm' : 'alert', message: f.message, life: 'detected',
    });
  }
  return [...byKey.values()].sort((a, b) =>
    (SEVERITY_RANK[b.severity] ?? 0) - (SEVERITY_RANK[a.severity] ?? 0)
    || (a.row?.first_seen ?? '').localeCompare(b.row?.first_seen ?? '')
    || a.key.localeCompare(b.key));
}

function Conditions({ conds, recent, onAck, acking }: {
  conds: Cond[]; recent: PlatformAlarmRow[]; onAck: (id: string) => void; acking: string | null;
}) {
  const operator = atLeast('operator');
  if (conds.length === 0) {
    const last = recent[0];
    return (
      <div className="ph-quiet">
        <span className="ph-dot tone-ok" aria-hidden />
        <div>
          <b>Nothing is wrong with the monitoring.</b>
          <span className="muted">
            {last
              ? ` The last condition, ${humanise(last.alarm_type).toLowerCase()} on ${last.instance}, closed ${relativeTime(last.cleared_at)}.`
              : ' No platform condition has closed in the last day either.'}
          </span>
        </div>
      </div>
    );
  }
  return (
    <ul className="ph-conds">
      {conds.map((c) => {
        const r = c.row;
        const hint = WHAT_TO_CHECK[c.alarm_type];
        return (
          <li key={c.key} className={`ph-cond tone-${toneOfSeverity(c.severity)}`}>
            <div className="ph-cond-head">
              <span className={`cls-pill ${c.response_class === 'alarm' ? 'alarm' : 'alert'}`}>
                {c.response_class === 'alarm' ? 'ALARM' : 'ALERT'}
              </span>
              <StatusChip status={c.severity} />
              <span className="ph-cond-name">{humanise(c.alarm_type)}</span>
              {c.instance && c.instance !== '*' && <span className="ph-cond-on">on {c.instance}</span>}
              <span className="ph-cond-life">
                {c.life === 'open' && r && <>since {clock(r.first_seen)} · {span(Date.now() - new Date(r.first_seen).getTime())}{r.occurrence_count > 1 ? ` · ${r.occurrence_count}×` : ''}</>}
                {c.life === 'detected' && <>detected now · not yet raised</>}
                {c.life === 'clearing' && r && <>no longer detected · closing{r.first_seen ? ` · open since ${clock(r.first_seen)}` : ''}</>}
              </span>
              {r && (r.acknowledged_at
                ? <span className="ph-ack-done">acknowledged {relativeTime(r.acknowledged_at)}</span>
                : operator && c.response_class === 'alarm' && (
                  <button type="button" className="ph-ack" disabled={acking === r.id} onClick={() => onAck(r.id)}>
                    {acking === r.id ? 'Acknowledging…' : 'Acknowledge'}
                  </button>
                ))}
            </div>
            <p className="ph-cond-msg">{c.message}</p>
            {hint && <p className="ph-cond-hint"><span>What to check</span>{hint}</p>}
          </li>
        );
      })}
    </ul>
  );
}

// ------------------------------------------------------------------- history

interface Episode { start: string; end: string; rows: PlatformAlarmRow[]; severity: string }

/** Rows that opened within 90 s of each other are one event: the sweep that
 *  marks every collector stale raises eight alarms in the same second. */
function episodes(recent: PlatformAlarmRow[]): Episode[] {
  const sorted = [...recent].sort((a, b) => b.first_seen.localeCompare(a.first_seen));
  const out: Episode[] = [];
  for (const r of sorted) {
    const cur = out[out.length - 1];
    if (cur && Math.abs(new Date(cur.rows[cur.rows.length - 1].first_seen).getTime() - new Date(r.first_seen).getTime()) <= 90_000) {
      cur.rows.push(r);
      if (r.first_seen < cur.start) cur.start = r.first_seen;
      if ((r.cleared_at ?? '') > cur.end) cur.end = r.cleared_at ?? cur.end;
      cur.severity = worstOf(cur.rows);
    } else {
      out.push({ start: r.first_seen, end: r.cleared_at ?? r.first_seen, rows: [r], severity: r.severity });
    }
  }
  return out;
}

function summary(rows: PlatformAlarmRow[]): string {
  const n = new Map<string, number>();
  for (const r of rows) n.set(r.alarm_type, (n.get(r.alarm_type) ?? 0) + 1);
  return [...n.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([t, c]) => `${c > 1 ? `${c} × ` : ''}${humanise(t).toLowerCase()}`)
    .join(', ');
}

function History({ recent }: { recent: PlatformAlarmRow[] }) {
  const eps = useMemo(() => episodes(recent), [recent]);
  if (eps.length === 0) return <p className="ph-note">No platform condition closed in the last 24 hours.</p>;
  return (
    <ol className="ph-episodes">
      {eps.map((e) => (
        <li key={e.start} className={`ph-episode tone-${toneOfSeverity(e.severity)}`}>
          <details>
            <summary>
              <span className="ph-ep-when">{clock(e.start)}</span>
              <span className="ph-ep-span">{span(new Date(e.end).getTime() - new Date(e.start).getTime())}</span>
              <span className="ph-ep-what">{summary(e.rows)}</span>
              <StatusChip status={e.severity} />
            </summary>
            <ul>
              {e.rows.map((r) => (
                <li key={r.id}>
                  <b>{humanise(r.alarm_type)}</b>{r.instance && r.instance !== '*' ? ` on ${r.instance}` : ''}
                  <span className="muted"> · {clock(r.first_seen)} to {r.cleared_at ? clock(r.cleared_at) : '—'}{r.occurrence_count > 1 ? ` · ${r.occurrence_count}×` : ''}</span>
                  <div className="muted small">{r.message}</div>
                </li>
              ))}
            </ul>
          </details>
        </li>
      ))}
    </ol>
  );
}

// ---------------------------------------------------------------------- page

export function PlatformHealth() {
  const qc = useQueryClient();
  const health = useQuery<CollectorHealth>({
    queryKey: ['collector-health'], queryFn: api.collectorHealth, refetchInterval: 15_000,
  });
  // The badge's own source: the same numbers the trust banner reads, so this
  // page can never disagree with the strip above the navigation.
  const platform = useQuery<PlatformState>({
    queryKey: ['platform-state'], queryFn: api.platformState, refetchInterval: 20_000, retry: false,
  });
  // Placement and build per collector, which the health read does not carry.
  const fleet = useQuery<CollectorsPage>({
    queryKey: ['collectors'], queryFn: () => api.collectors(), staleTime: 60_000, retry: false,
  });
  const socket = useSocketStatus();
  const ack = useMutation({
    mutationFn: (id: string) => api.acknowledgeAlarm(id),
    onSettled: () => qc.invalidateQueries({ queryKey: ['collector-health'] }),
  });

  const data = health.data;
  const conds = useMemo(() => (data ? mergeConditions(data) : []), [data]);

  if (health.isLoading) return <p className="muted">Loading…</p>;
  if (health.error || !data) {
    // Failing to load THIS page is itself the finding worth stating plainly.
    return (
      <Notice tone="error">
        Could not read the platform's own health: {String(health.error ?? 'no answer')}. Nothing on
        any other page can be trusted to be current until this loads.
      </Notice>
    );
  }

  const p = data.pipeline;
  const state = platform.data?.state ?? (data.verdict.healthy ? 'ok' : 'impaired');
  const trusted = platform.data?.telemetry_trusted_s ?? 300;
  const stale = platform.data?.telemetry_stale ?? !p.telemetry_present;
  const fleetById = new Map((fleet.data?.collectors ?? []).map((c) => [c.id, c]));
  const staleCollectors = data.collectors.filter((c) => c.heartbeat_age_seconds === null || c.heartbeat_age_seconds >= c.stale_after_seconds);
  const stranded = data.shards?.owned_by_unhealthy ?? 0;
  const unassigned = data.shards?.unassigned ?? 0;
  const ownedTotal = Object.values(data.shards?.owned ?? {}).reduce((a, b) => a + b, 0);
  const pending = Object.entries(p.stream_pending);
  const pendingTotal = pending.reduce((a, [, n]) => a + n, 0);
  const lagTone: Tone = p.ingest_lag_seconds === null ? 'unknown'
    : p.ingest_lag_seconds >= p.lag_critical_seconds ? 'critical'
      : p.ingest_lag_seconds >= p.lag_warning_seconds ? 'warn' : 'ok';
  const newestHb = data.collectors.reduce<number | null>((m, c) => (c.heartbeat_age_seconds === null ? m : m === null ? c.heartbeat_age_seconds : Math.min(m, c.heartbeat_age_seconds)), null);

  const stages: Stage[] = [
    {
      key: 'collectors', name: 'Collectors', to: '/settings/collectors',
      tone: data.collectors.length === 0 ? 'critical' : staleCollectors.length || stranded ? 'critical' : 'ok',
      value: data.collectors.length === 0 ? 'none registered'
        : `${data.collectors.length - staleCollectors.length} of ${data.collectors.length} reporting`,
      detail: data.collectors.length === 0 ? 'nothing is polling the fleet'
        : stranded ? `${stranded} endpoints owned by a collector that is not answering`
          : `newest heartbeat ${secs(newestHb)} ago · stale after ${secs(data.collectors[0].stale_after_seconds)}`,
    },
    {
      key: 'queue', name: 'Queue',
      // A backlog is the same symptom the workers show; an empty queue is
      // fine whatever the lag says.
      tone: pendingTotal > 0 && lagTone !== 'unknown' ? lagTone : 'ok',
      value: `${pendingTotal} waiting`,
      detail: pending.length ? pending.map(([s, n]) => `${s.replace(/\.v\d+$/, '')} ${n}`).join(' · ') + ' · taken, not yet written' : 'taken, not yet written',
    },
    {
      key: 'workers', name: 'Ingest workers',
      tone: p.worker_heartbeat_age_seconds === null ? 'critical' : lagTone,
      value: p.worker_heartbeat_age_seconds === null ? 'no heartbeat' : `${secs(p.ingest_lag_seconds)} behind`,
      detail: p.worker_heartbeat_age_seconds === null
        ? 'checked by the API, because a dead worker cannot report itself'
        : `publish to committed row · warn at ${secs(p.lag_warning_seconds)}, critical at ${secs(p.lag_critical_seconds)}`,
    },
    {
      key: 'db', name: 'Database',
      tone: !p.telemetry_present ? 'critical' : stale ? 'critical' : 'ok',
      value: !p.telemetry_present ? 'nothing ever written' : `${secs(p.telemetry_age_seconds)} old`,
      detail: !p.telemetry_present ? 'every empty chart elsewhere means this, not a quiet datacenter'
        : `newest sample · bounded by the poll interval · trusted up to ${secs(trusted)}`,
    },
    {
      key: 'browser', name: 'This browser',
      tone: socket === 'open' ? 'ok' : socket === 'connecting' ? 'unknown' : 'warn',
      value: socket === 'open' ? 'live feed open' : `live feed ${socket}`,
      detail: socket === 'open' ? 'pushes every change to the open tabs' : 'pages will not change until it reconnects',
    },
  ];

  const verdict = state === 'ok'
    ? `Monitoring OK. Telemetry is ${secs(p.telemetry_age_seconds)} old; the platform vouches for it up to ${secs(trusted)}.`
    : state === 'blind'
      ? `Blind. ${stale ? `The newest reading is ${secs(p.telemetry_age_seconds)} old, past the ${secs(trusted)} the platform trusts. ` : ''}Nothing on the other pages describes the estate as it is now.`
      : state === 'impaired'
        ? `Impaired. ${data.verdict.summary}`
        : `Degraded. ${data.verdict.summary} The numbers are still arriving.`;

  return (
    <div className="ph">
      <header className="ph-head">
        <h2>Platform health</h2>
        <p className={`ph-verdict state-${state}`}><span className="ph-dot" aria-hidden />{verdict}</p>
      </header>

      <Pipeline stages={stages} />

      <section className="ph-section">
        <h3>Open conditions{conds.length > 0 && <span className="count"> {conds.length}</span>}</h3>
        <Conditions conds={conds} recent={data.recent ?? []} acking={ack.isPending ? (ack.variables ?? null) : null}
                    onAck={(id) => ack.mutate(id)} />
        {ack.isError && <Notice tone="error">Could not acknowledge: {String(ack.error)}</Notice>}
      </section>

      <section className="ph-section">
        <h3>Collectors<span className="count"> {data.collectors.length}</span></h3>
        {data.shards && data.collectors.length > 0 && (
          <p className={`ph-shards${stranded ? ' is-bad' : ''}`}>
            {ownedTotal.toLocaleString()} endpoints assigned across {data.shards.collectors} collectors
            {unassigned ? <> · <b>{unassigned} that no collector can own</b></> : ' · none unassigned'}
            {stranded
              ? <> · <b>{stranded} owned by a collector that is not answering</b>, which nobody else will poll</>
              : ' · every shard has a live owner'}
          </p>
        )}
        {data.collectors.length === 0 ? (
          <Notice tone="error">No collector has ever checked in. Nothing is polling the fleet.</Notice>
        ) : (
          <table className="ph-table">
            <thead>
              <tr><th>Collector</th><th>Pool</th><th>Heartbeat</th><th>Status</th><th>Endpoints</th><th>Build</th></tr>
            </thead>
            <tbody>
              {[...data.collectors].sort((a, b) => a.collector_id.localeCompare(b.collector_id)).map((c) => {
                const isStale = c.heartbeat_age_seconds === null || c.heartbeat_age_seconds >= c.stale_after_seconds;
                const row = fleetById.get(c.collector_id);
                const share = c.endpoints_owned > 0 ? Math.min(1, c.endpoints_online / c.endpoints_owned) : 0;
                return (
                  <tr key={c.collector_id} className={isStale ? 'is-stale' : undefined}>
                    <td className="mono"><Link to={`/settings/collectors/${encodeURIComponent(c.collector_id)}`}>{c.collector_id}</Link></td>
                    <td className="muted">{row?.pool_name ?? row?.site ?? '—'}{row?.pool_plane ? <span className="small"> · {row.pool_plane}</span> : ''}</td>
                    <td className={isStale ? 'critical' : undefined}>
                      {c.heartbeat_age_seconds === null ? 'never' : `${secs(c.heartbeat_age_seconds)} ago`}
                    </td>
                    <td><span className={`chip ${isStale ? 'critical' : 'ok'}`}><span className="dot" aria-hidden />{isStale ? 'stale' : (c.status ?? 'unknown').toLowerCase()}</span></td>
                    <td className="ph-eps">
                      <span className="num">{c.endpoints_online}</span><span className="muted"> of {c.endpoints_owned} online</span>
                      {c.endpoints_online > c.endpoints_owned
                        ? <span className="warn"> · counts disagree</span>
                        : <span className="ph-bar" aria-hidden><span style={{ width: `${share * 100}%` }} /></span>}
                    </td>
                    <td className="muted mono small">{row?.build ?? '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <section className="ph-section">
        <h3>Last 24 hours</h3>
        <History recent={data.recent ?? []} />
      </section>
    </div>
  );
}
