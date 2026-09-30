import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  ApiError,
  api,
  type CollectorDetail as Detail,
  type CollectorEndpoint,
  type PreflightCheck,
} from '../../api/client';
import { humanise, oneLine, relativeTime, untilTime } from '../../lib/format';
import { Tip } from '../../components/HoverTip';
import { StatusChip } from '../../components/StatusChip';
import { usePaged } from '../../components/Pagination';
import { KpiBand, type Kpi } from '../../components/estate';
import { StateBadge } from './Collectors';

/** One collector (docs/26 Phase 8): who it is, whether it is well, and what
 *  it is responsible for.
 *
 *  Read-only on purpose. Every action on a collector - placement, drain,
 *  tokens, certificates, config - lives on the Collectors page's Manage
 *  sheet already; a second set of buttons here would be a second place for
 *  the rules to drift. This page answers the question that sheet cannot:
 *  "it says healthy, so why is this device not being polled?" */
export function CollectorDetail() {
  const { id = '' } = useParams();
  const page = useQuery<Detail>({
    queryKey: ['collector', id],
    queryFn: () => api.collectorDetail(id),
    refetchInterval: 15_000,
  });

  if (page.isLoading) return <p className="muted">Loading…</p>;
  if (page.error) {
    return (
      <div className="stack">
        <p><Link to="/settings/collectors">← Collectors</Link></p>
        <div className="banner">
          {page.error instanceof ApiError && page.error.status === 404
            ? <>No collector called <span className="mono">{id}</span>.</>
            : 'Could not load this collector.'}
        </div>
      </div>
    );
  }
  const d = page.data!;

  return (
    <div className="stack">
      <p><Link to="/settings/collectors">← Collectors</Link></p>
      <div>
        <h2 className="mono">{d.id}</h2>
        <p className="subtitle">
          <StateBadge row={d} />
          {' · '}
          {d.pool_name
            ? <>pool <strong>{d.pool_name}</strong> ({d.site})</>
            : d.site ? <>site {d.site}, no pool</> : 'no site or pool - serves every site'}
          {' · '}
          {!d.has_run ? 'never checked in'
            : d.alive ? `heartbeat ${relativeTime(d.last_heartbeat)}`
              : <span className="warn">silent · {relativeTime(d.last_heartbeat)}</span>}
        </p>
      </div>

      <KpiBand items={kpis(d)} />

      {d.reported_elsewhere > 0 && (
        <div className="banner soft">
          <Tip tip={oneLine(`The current plan gives these endpoints to this collector,
                  but another collector sent their last report. Normal for a
                  minute or two during a move, drain or failover. If it does not
                  fall to zero, the move never happened.`)}>
            {d.reported_elsewhere} of its endpoints were last reported by another collector.
          </Tip>
        </div>
      )}

      <div style={{ display: 'grid', gap: 16,
                    gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))' }}>
        <Identity d={d} />
        <Heartbeat d={d} />
      </div>

      <Capacity d={d} />
      <Preflight d={d} />
      <RecentErrors d={d} />
      <Owned endpoints={d.endpoints} byProtocol={d.by_protocol} />
    </div>
  );
}

function kpis(d: Detail): Kpi[] {
  const cap = d.capacity;
  const online = d.by_status.ONLINE ?? 0;
  const owned = d.endpoints.length;
  const failedPct = d.stats.polls_total
    ? (100 * (d.stats.polls_failed ?? 0)) / d.stats.polls_total : null;
  return [
    { caption: 'Owned', value: owned, digits: 0,
      why: owned === 0 ? 'The current plan gives it nothing' : null },
    { caption: 'Online', value: owned ? (100 * online) / owned : null, unit: '%', digits: 0,
      tone: !owned ? undefined : online === owned ? 'ok' : online / owned > 0.95 ? 'warn' : 'critical',
      why: owned ? null : 'Nothing owned' },
    { caption: 'Errors', value: d.error_count, digits: 0,
      tone: d.error_count ? 'warn' : 'ok' },
    { caption: 'Polls failed', value: failedPct, unit: '%', digits: 2,
      why: failedPct == null ? 'Its heartbeat reports no poll counters' : null },
    { caption: 'Workers busy', value: cap?.busy_pct ?? null, unit: '%', digits: 0,
      tone: cap == null ? undefined : cap.shed > 0 || cap.busy_pct >= 95 ? 'critical'
        : cap.busy_pct >= 85 ? 'warn' : 'ok',
      why: cap == null ? 'It has not sent a capacity report yet' : null },
  ];
}

function Row({ label, children, tip }: {
  label: string; children: React.ReactNode; tip?: string;
}) {
  return (
    <tr>
      <th style={{ textAlign: 'left', fontWeight: 'normal' }} className="muted">
        {tip ? <Tip tip={tip}>{label}</Tip> : label}
      </th>
      <td>{children}</td>
    </tr>
  );
}

const dash = <span className="muted">—</span>;

const SKEW: Record<Detail['version_skew'], { cls: string; text: string; tip: string }> = {
  current: { cls: 'ok', text: 'current', tip: 'Same release as the platform, or newer.' },
  supported: { cls: 'ok', text: 'one release behind', tip: 'N-1: fully supported.' },
  outdated: { cls: 'warn', text: 'two releases behind',
              tip: 'N-2: keeps collecting, but is given no new work. Upgrade it.' },
  rejected: { cls: 'critical', text: 'too old',
              tip: 'Older than N-2 or a major version behind. Upgrade it.' },
  unknown: { cls: 'muted', text: 'unknown',
             tip: 'A dev build, or a collector that never reported a version.' },
};

function Identity({ d }: { d: Detail }) {
  const skew = SKEW[d.version_skew];
  const certOk = d.cert_serial && !d.cert_revoked_at;
  return (
    <fieldset className="proto">
      <legend>Identity</legend>
      <table>
        <tbody>
          <Row label="Host">{d.hostname ?? dash}</Row>
          <Row label="Build">
            <span className="mono">{d.build ?? '—'}</span>{' '}
            <Tip className={skew.cls} tip={skew.tip}>
              ({skew.text}; platform {d.platform_version})
            </Tip>
          </Row>
          <Row label="Certificate"
               tip="mTLS identity (docs/26 Phase 2). None means it is on its bearer token alone.">
            {!d.cert_serial ? <span className="muted">never enrolled — bearer token only</span>
              : d.cert_revoked_at ? <span className="critical">revoked {relativeTime(d.cert_revoked_at)}</span>
                : <>serial <span className="mono">{d.cert_serial}</span></>}
          </Row>
          {certOk && (
            <Row label="Expires">{untilTime(d.cert_not_after)}</Row>
          )}
          <Row label="Enrolled">
            {d.enrolled_at ? <>{relativeTime(d.enrolled_at)}{d.enrolled_by ? ` by ${d.enrolled_by}` : ''}</> : dash}
          </Row>
          <Row label="Token generation">{d.token_generation}</Row>
          <Row label="Started">{d.started_at ? relativeTime(d.started_at) : dash}</Row>
        </tbody>
      </table>
    </fieldset>
  );
}

function num(v: number | null | undefined, digits = 0): React.ReactNode {
  return v == null ? dash : v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function bytes(v: number | null | undefined): React.ReactNode {
  if (v == null) return dash;
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let n = v;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function seconds(v: number | null | undefined): React.ReactNode {
  if (v == null) return dash;
  if (v < 120) return `${Math.round(v)} s`;
  if (v < 7200) return `${Math.round(v / 60)} min`;
  return `${(v / 3600).toFixed(1)} h`;
}

function Heartbeat({ d }: { d: Detail }) {
  const s = d.stats;
  return (
    <fieldset className="proto">
      <legend>
        <Tip tip={oneLine(`What its last heartbeat said. A dash is a figure it did not
                report - a collector on the Redis transport has no spool at all -
                never a zero nobody measured.`)}>Heartbeat</Tip>
      </legend>
      <table>
        <tbody>
          <Row label="Polls">{num(s.polls_total)} ({num(s.polls_failed)} failed)</Row>
          <Row label="Traps / events">{num(s.traps_received)} / {num(s.events_received)}</Row>
          <Row label="Work queue">{num(s.queue_depth)} of {num(s.queue_capacity)}</Row>
          <Row label="Spool" tip="Gateway transport only: telemetry buffered to disk while the core is unreachable.">
            {bytes(s.spool_bytes)}{s.spool_oldest_age_s != null && <> · oldest {seconds(s.spool_oldest_age_s)}</>}
          </Row>
          <Row label="Replay rate">{s.replay_rate == null ? dash : `${num(s.replay_rate)}/s`}</Row>
          <Row label="Assignment">
            v{num(s.assignment_version)}{s.assignment_age_s != null && <> · {seconds(s.assignment_age_s)} old</>}
          </Row>
          <Row label="Streams">{num(s.active_streams)}</Row>
          <Row label="Mapping bundle">
            {s.mapping_bundle_sha ? <span className="mono">{s.mapping_bundle_sha.slice(0, 12)}</span> : dash}
          </Row>
        </tbody>
      </table>
    </fieldset>
  );
}

/** docs/26 Phase 5. Measured, not estimated: the busy figure is worker time
 *  held over worker time available, and each protocol's cost per poll is
 *  what its polls actually took - the weight a sizing table would guess. */
function Capacity({ d }: { d: Detail }) {
  const c = d.capacity;
  const protos = c ? Object.entries(c.protocols) : [];
  const tone = !c ? '' : c.shed > 0 || c.busy_pct >= 95 ? 'critical'
    : c.busy_pct >= 85 ? 'warn' : 'ok';
  return (
    <fieldset className="proto">
      <legend>
        <Tip tip={oneLine(`Its poll workers over the last few minutes. 85% busy warns,
                95% or any shed poll is major. Streams and trap listeners hold no
                poll worker, so they are not in these figures.`)}>Capacity</Tip>
      </legend>
      {!c ? (
        <p className="muted" style={{ margin: 0 }}>
          Not reported yet. A collector sends this from its second heartbeat on;
          one built before capacity reporting never will.
        </p>
      ) : (
        <>
          <p style={{ marginTop: 0 }}>
            <span className={tone}>{c.busy_pct.toFixed(0)}% of {c.workers} workers busy</span>
            <span className="muted">
              {' '}over {seconds(c.window_s)} · {num(c.polls_per_s, 2)} of{' '}
              {num(c.scheduled_polls_per_s, 2)} scheduled polls/s · {num(c.points_per_s, 1)} points/s
              · queue wait {num(c.queue_wait_avg_ms, 1)} ms
            </span>
          </p>
          {(c.shed > 0 || c.late > 0 || c.overrun > 0) && (
            <p className="muted">
              {c.shed > 0 && <span className="critical">{c.shed} shed (never made) · </span>}
              {c.late > 0 && <span className="warn">{c.late} started over 5 s late · </span>}
              <Tip tip={oneLine(`Due again while the previous poll of the same endpoint
                      was still running: usually one slow device, not the pool.`)}>
                {c.overrun} overran
              </Tip>
            </p>
          )}
          <div className="table-frame">
            <table>
              <thead>
                <tr>
                  <th>Protocol</th><th className="num">Endpoints</th>
                  <th className="num">Polls/s</th><th className="num">Points/s</th>
                  <th className="num">
                    <Tip tip="This protocol's share of all worker time">Busy</Tip>
                  </th>
                  <th className="num">
                    <Tip tip="Worker-seconds one poll takes, measured. Timeouts and retries count.">
                      Cost/poll
                    </Tip>
                  </th>
                  <th className="num">
                    <Tip tip={oneLine(`Share of its worker time spent waiting for a
                            protocol or per-host slot. High means the protocol
                            limit is the ceiling, not the worker pool.`)}>Slot wait</Tip>
                  </th>
                  <th className="num">Limit</th>
                </tr>
              </thead>
              <tbody>
                {protos.map(([name, p]) => (
                  <tr key={name}>
                    <td>{name}</td>
                    <td className="num">{p.jobs}</td>
                    <td className="num">{num(p.polls_per_s, 2)}</td>
                    <td className="num">{num(p.points_per_s, 1)}</td>
                    <td className="num">{p.busy_pct.toFixed(1)}%</td>
                    <td className="num">{p.cost_s_per_poll.toFixed(2)} s</td>
                    <td className={p.sem_wait_pct >= 50 ? 'num warn' : 'num'}>
                      {p.sem_wait_pct.toFixed(0)}%
                    </td>
                    <td className="num muted">{p.limit || 'pool'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </fieldset>
  );
}

const CHECK_LABEL: Record<string, string> = {
  ntp_offset: 'Clock (NTP)', spool_disk: 'Spool disk', trap_port: 'Trap port',
  core_tls: 'Core over TLS', reachability: 'Reachability',
};

function checkLabel(c: PreflightCheck): string {
  if (CHECK_LABEL[c.check]) return CHECK_LABEL[c.check];
  if (c.check.startsWith('reach_')) return `Reach ${humanise(c.check.slice(6))}`;
  return humanise(c.check);
}

const CHECK_CLASS: Record<PreflightCheck['status'], string> = {
  ok: 'ok', warn: 'warn', fail: 'critical', skipped: 'muted',
};

export function Preflight({ d }: { d: Pick<Detail, 'preflight'> }) {
  const p = d.preflight;
  return (
    <fieldset className="proto">
      <legend>
        <Tip tip={oneLine(`Its own self-check, run after enrolling and on demand with
                \`dcim-collector preflight\`. A skipped check is one it could not
                honestly run, and says why.`)}>Preflight</Tip>
      </legend>
      {!p ? (
        <p className="muted">
          Never run. It runs automatically after <span className="mono">dcim-collector
          enroll</span>, or on demand with <span className="mono">dcim-collector preflight</span>.
        </p>
      ) : (
        <>
          <p>
            {p.passed ? <span className="ok">Passed</span> : <span className="warn">Findings</span>}
            {' '}<span className="muted">{relativeTime(p.ran_at)}</span>
          </p>
          <table>
            <thead><tr><th>Check</th><th>Result</th><th>Detail</th></tr></thead>
            <tbody>
              {p.checks.map((c) => (
                <tr key={c.check}>
                  <td>{checkLabel(c)}</td>
                  <td className={CHECK_CLASS[c.status]}>{c.status}</td>
                  <td className="muted">{c.detail || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </fieldset>
  );
}

function RecentErrors({ d }: { d: Detail }) {
  return (
    <fieldset className="proto">
      <legend>Recent errors{d.error_count > d.recent_errors.length
        ? ` (newest ${d.recent_errors.length} of ${d.error_count})` : ''}</legend>
      {d.recent_errors.length === 0 ? (
        <p className="muted">None of its endpoints is carrying an error.</p>
      ) : (
        <div className="table-frame">
          <table>
            <thead>
              <tr><th>Device</th><th>Protocol</th><th>Error</th><th>When</th></tr>
            </thead>
            <tbody>
              {d.recent_errors.map((e) => (
                <tr key={e.id}>
                  <td><Link to={`/devices/${e.device_id}`}>{e.device_name}</Link></td>
                  <td className="muted">{humanise(e.protocol)}</td>
                  <td>{e.last_error_class ? `${e.last_error_class}: ` : ''}{e.last_error}</td>
                  <td className="muted">{relativeTime(e.last_failure)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </fieldset>
  );
}

function Owned({ endpoints, byProtocol }: {
  endpoints: CollectorEndpoint[]; byProtocol: Record<string, number>;
}) {
  const [proto, setProto] = useState('');
  const [problemsOnly, setProblemsOnly] = useState(false);
  const shown = useMemo(() => endpoints.filter((e) =>
    (!proto || e.protocol === proto) && (!problemsOnly || e.status !== 'ONLINE')),
  [endpoints, proto, problemsOnly]);
  const { rows, foot } = usePaged(shown, { noun: 'endpoints' });

  return (
    <fieldset className="proto">
      <legend>What it owns ({endpoints.length.toLocaleString()})</legend>
      {endpoints.length === 0 ? (
        <p className="muted">
          The current plan gives it nothing — pending, draining, placed in a pool with no
          endpoints, or outranked by the other collectors at its site.
        </p>
      ) : (
        <>
          <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 8 }}>
            <label>
              <span className="muted">Protocol </span>
              <select value={proto} onChange={(e) => setProto(e.target.value)}>
                <option value="">All</option>
                {Object.entries(byProtocol).map(([k, n]) => (
                  <option key={k} value={k}>{humanise(k)} ({n})</option>
                ))}
              </select>
            </label>
            <label>
              <input type="checkbox" checked={problemsOnly}
                     onChange={(e) => setProblemsOnly(e.target.checked)} />
              {' '}Not online only
            </label>
          </div>
          <div className="table-frame">
            <table>
              <thead>
                <tr><th>Device</th><th>Protocol</th><th>Address</th><th>Status</th>
                  <th>Seen</th><th className="num">Latency</th></tr>
              </thead>
              <tbody>
                {rows.map((e) => (
                  <tr key={e.id}>
                    <td><Link to={`/devices/${e.device_id}`}>{e.device_name}</Link></td>
                    <td className="muted">{humanise(e.protocol)}</td>
                    <td className="mono">{e.address ?? '—'}{e.port ? `:${e.port}` : ''}</td>
                    <td><StatusChip status={e.status} /></td>
                    <td className="muted">{relativeTime(e.last_seen)}</td>
                    <td className="num muted">
                      {e.last_latency_ms != null ? `${e.last_latency_ms} ms` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {rows.length === 0 && <p className="muted">Nothing matches.</p>}
          {foot}
        </>
      )}
    </fieldset>
  );
}
