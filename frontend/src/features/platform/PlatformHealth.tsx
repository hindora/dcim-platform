import { useQuery } from '@tanstack/react-query';
import { api, type CollectorHealth } from '../../api/client';

/** Is the monitoring working?
 *
 *  Every other page in this application answers a question about the
 *  datacenter, and every one of them shows the same thing when the collector
 *  dies as when the datacenter is quiet: no alarms, flat charts, nothing
 *  moving. This page is the one that can tell those apart, which is the only
 *  reason it exists.
 *
 *  It shows the two latency numbers separately and says which is which,
 *  because they are not interchangeable. Pipeline lag is publish-to-commit and
 *  sits under a second in health. Telemetry age is bounded by the poll
 *  interval and is routinely a minute or two on a healthy fleet - judged
 *  against the lag thresholds it would look permanently broken.
 */

const SEVERITY_TONE: Record<string, string> = {
  CRITICAL: 'critical',
  MAJOR: 'warn',
  WARNING: 'warn',
};

function secs(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  if (v < 1) return `${(v * 1000).toFixed(0)} ms`;
  if (v < 90) return `${v.toFixed(1)} s`;
  if (v < 5400) return `${(v / 60).toFixed(1)} min`;
  return `${(v / 3600).toFixed(1)} h`;
}

function LagTile({ label, value, detail, warn, critical }: {
  label: string; value: number | null; detail: string;
  warn?: number; critical?: number;
}) {
  const tone = value === null ? 'unknown'
    : critical !== undefined && value >= critical ? 'critical'
      : warn !== undefined && value >= warn ? 'warn' : 'ok';
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className={`value ${tone === 'unknown' ? 'muted' : tone}`}>
        {secs(value)}
      </div>
      <div className="detail">{detail}</div>
    </div>
  );
}

export function PlatformHealth() {
  const { data, error, isLoading } = useQuery<CollectorHealth>({
    queryKey: ['collector-health'],
    queryFn: api.collectorHealth,
    refetchInterval: 15_000,
  });

  if (isLoading) return <p className="muted">Loading…</p>;
  if (error) {
    // Failing to load THIS page is itself the finding worth stating plainly.
    return (
      <div className="banner">
        Could not read the platform's own health: {String(error)}. Nothing on
        any other page can be trusted to be current until this loads.
      </div>
    );
  }
  if (!data) return null;

  const p = data.pipeline;

  return (
    <>
      <h2>Platform health</h2>
      <p className="subtitle">
        Whether the monitoring is working. Every other page shows the same thing
        during a collector outage as during a quiet night.
      </p>

      <div className={`verdict${data.verdict.healthy ? '' : ' unhealthy'}`}>
        <div className="verdict-label">Self-monitoring</div>
        <div className={`verdict-value ${data.verdict.healthy ? 'ok'
          : SEVERITY_TONE[data.verdict.severity ?? ''] ?? 'warn'}`}>
          {data.verdict.healthy ? 'Healthy'
            : `${data.verdict.count ?? 1} finding${(data.verdict.count ?? 1) > 1 ? 's' : ''}`}
        </div>
        <p className="verdict-reason">{data.verdict.summary}</p>
      </div>

      <div className="tiles">
        <LagTile label="Pipeline lag" value={p.ingest_lag_seconds}
                 warn={p.lag_warning_seconds} critical={p.lag_critical_seconds}
                 detail={`collector publish to committed row · warn at ${
                   p.lag_warning_seconds}s`} />
        <LagTile label="Telemetry age" value={p.telemetry_age_seconds}
                 detail="newest sample · bounded by the poll interval, not by zero" />
        <LagTile label="Worker heartbeat" value={p.worker_heartbeat_age_seconds}
                 detail="checked by the API, because a dead worker cannot report itself" />
        <div className="tile">
          <div className="label">Stream backlog</div>
          <div className="value">
            {Object.values(p.stream_pending).reduce((a, b) => a + b, 0)}
          </div>
          <div className="detail">
            delivered, not acknowledged — climbs before lag does
          </div>
        </div>
      </div>

      {!p.telemetry_present && (
        <div className="banner">
          No telemetry has ever been written. Nothing on this platform is
          measuring anything, and every empty chart elsewhere means that rather
          than a quiet datacenter.
        </div>
      )}

      {data.findings.length > 0 && (
        <>
          <h3>Findings</h3>
          <table>
            <thead>
              <tr><th>Type</th><th>Instance</th><th>Severity</th><th>What it means</th></tr>
            </thead>
            <tbody>
              {data.findings.map((f) => (
                <tr key={`${f.alarm_type}|${f.instance}`}>
                  <td>{f.alarm_type.replace(/_/g, ' ')}</td>
                  <td className="muted">{f.instance}</td>
                  <td>
                    <span className={`chip ${SEVERITY_TONE[f.severity] ?? 'unknown'}`}>
                      {f.severity}
                    </span>
                  </td>
                  <td className="muted small">{f.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">
            These are raised into the same alarm list as device faults, with no
            device attached. An operator should not have to visit a second
            screen to discover the monitoring is broken.
          </p>
        </>
      )}

      <h3>Collectors</h3>
      {data.collectors.length === 0 ? (
        <div className="banner">
          No collector has ever checked in. Nothing is polling the fleet.
        </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Collector</th><th>Last heartbeat</th><th>Status</th>
              <th>Endpoints</th><th>Online</th>
            </tr>
          </thead>
          <tbody>
            {data.collectors.map((c) => {
              const stale = c.heartbeat_age_seconds === null
                || c.heartbeat_age_seconds >= c.stale_after_seconds;
              return (
                <tr key={c.collector_id}>
                  <td>{c.collector_id}</td>
                  <td className={stale ? 'critical' : 'ok'}>
                    {secs(c.heartbeat_age_seconds)} ago
                  </td>
                  <td>
                    <span className={`chip ${stale ? 'critical' : 'ok'}`}>
                      {stale ? 'stale' : (c.status ?? 'unknown')}
                    </span>
                  </td>
                  <td className="num">{c.endpoints_owned}</td>
                  <td className="num">
                    {c.endpoints_online}
                    {/* Only when the ratio is meaningful. Live, this collector
                        reported 1386 online against 1340 owned, and printing
                        "103%" teaches an operator to stop reading the number
                        instead of asking which count is wrong. */}
                    {c.endpoints_owned > 0 && c.endpoints_online <= c.endpoints_owned && (
                      <span className="muted">
                        {' '}({((c.endpoints_online / c.endpoints_owned) * 100).toFixed(0)}%)
                      </span>
                    )}
                    {c.endpoints_online > c.endpoints_owned && (
                      <span className="warn"> counts disagree</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {data.open_alarms.length > 0 && (
        <>
          <h3>Open platform alarms</h3>
          <table>
            <thead>
              <tr><th>Type</th><th>Instance</th><th>Severity</th><th>Since</th><th>Seen</th></tr>
            </thead>
            <tbody>
              {data.open_alarms.map((a) => (
                <tr key={a.id}>
                  <td>{a.alarm_type.replace(/_/g, ' ')}</td>
                  <td className="muted">{a.instance}</td>
                  <td>
                    <span className={`chip ${SEVERITY_TONE[a.severity] ?? 'unknown'}`}>
                      {a.severity}
                    </span>
                  </td>
                  <td className="muted small">
                    {new Date(a.first_seen).toLocaleString()}
                  </td>
                  <td className="num">{a.occurrence_count}×</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}
