import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import {
  api,
  type DeviceDetail as Detail,
  type DeviceState,
  type EndpointSummary,
} from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { DeviceHistory } from './DeviceHistory';
import { formatMetric, metricLabel, relativeTime } from '../../lib/format';

export function DeviceDetail() {
  const { id = '' } = useParams();

  const device = useQuery<Detail>({
    queryKey: ['device', id],
    queryFn: () => api.device(id),
    enabled: Boolean(id),
  });

  const state = useQuery<DeviceState>({
    queryKey: ['device-state', id],
    queryFn: () => api.deviceState(id),
    enabled: Boolean(id),
    refetchInterval: 10_000,
    // A device with no telemetry yet is a normal state, not an error.
    retry: false,
  });

  if (device.isLoading) return <p className="muted">Loading…</p>;
  if (device.error) return <div className="banner">Failed to load: {String(device.error)}</div>;
  if (!device.data) return null;

  const d = device.data;
  const metrics = state.data?.metrics ?? {};

  return (
    <div className="stack">
      <div>
        <h2>{d.name}</h2>
        <p className="subtitle">
          {d.device_type} · {d.vendor ?? 'unknown vendor'} {d.model ?? ''}
        </p>
        <StatusChip status={d.status} />
      </div>

      <section>
        <h3>Identity and location</h3>
        <dl className="kv">
          <dt>Device type</dt><dd>{d.device_type}</dd>
          <dt>Vendor</dt><dd>{d.vendor ?? '—'}</dd>
          <dt>Model</dt><dd>{d.model ?? '—'}</dd>
          <dt>Serial</dt><dd className="mono">{d.serial_number ?? '—'}</dd>
          <dt>Management IP</dt><dd className="mono">{d.mgmt_ip ?? '—'}</dd>
          <dt>Production IP</dt><dd className="mono">{d.primary_ip ?? '—'}</dd>
          <dt>Datacenter</dt><dd>{d.location.datacenter_code ?? '—'}</dd>
          <dt>Room</dt><dd>{d.location.room_name ?? '—'}</dd>
          <dt>Row / Rack</dt>
          <dd>
            {d.location.row_name ?? '—'} / {d.location.rack_name ?? '—'}
            {d.location.u_start ? ` · U${d.location.u_start}` : ''}
          </dd>
          <dt>Lifecycle</dt><dd>{d.lifecycle}</dd>
        </dl>
      </section>

      <section>
        <h3>Communication</h3>
        <p className="muted">
          One row per protocol endpoint. A server has an OS agent and a BMC:
          they are separate agents and fail independently. <em>Seen</em> is the
          last poll attempt and <em>Success</em> the last one that answered — a
          fresh <em>Seen</em> beside a stale <em>Success</em> is an endpoint
          being polled and failing, which is a different fault from one nothing
          is polling at all. Poll and failure totals are cumulative for the life
          of the endpoint, not a recent window.
        </p>
        <table>
          <thead>
            <tr>
              <th>Protocol</th><th>Role</th><th>Address</th><th>Status</th>
              <th>Credential</th><th className="num">Interval</th>
              <th>Seen</th><th>Success</th><th className="num">Latency</th>
              <th className="num">Polls</th><th>Last error</th>
            </tr>
          </thead>
          <tbody>
            {d.endpoints.map((e) => (
              <tr key={e.id}>
                <td>{e.protocol}</td>
                <td className="muted">{e.role}</td>
                <td className="mono">{e.address}{e.port ? `:${e.port}` : ''}</td>
                <td><StatusChip status={e.status} /></td>
                {/* Only ever the hint - the secret itself never leaves the server. */}
                <td className="muted mono">{e.credential_hint ?? '—'}</td>
                <td className="num">{e.poll_interval_s ? `${e.poll_interval_s}s` : '—'}</td>
                <td className="muted">{relativeTime(e.last_seen)}</td>
                <td className="muted">{relativeTime(e.last_success)}</td>
                <td className="num muted">
                  {e.last_latency_ms != null ? `${e.last_latency_ms} ms` : '—'}
                </td>
                <td className="num"><PollCounts endpoint={e} /></td>
                <td className="muted">
                  {e.last_error
                    ? `${e.last_error_class ?? 'error'}: ${e.last_error}`
                    : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {d.endpoints.length === 0 && (
          <p className="muted">No endpoints configured for this device.</p>
        )}
      </section>

      <section>
        <h3>Current metrics</h3>
        {Object.keys(metrics).length === 0 ? (
          <p className="muted">
            No telemetry yet. The first poll lands within one interval of the
            collector picking this endpoint up.
          </p>
        ) : (
          <table>
            <thead>
              <tr><th>Metric</th><th className="num">Value</th><th>Quality</th><th>Age</th></tr>
            </thead>
            <tbody>
              {Object.entries(metrics).map(([key, m]) => (
                <tr key={key}>
                  <td>{metricLabel(key)}</td>
                  <td className="num">{formatMetric(key, m.v)}</td>
                  <td className="muted">{m.q}</td>
                  <td className="muted">{relativeTime(m.t)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {/* Charted from the same metric keys the device is actually reporting,
          so a device with no telemetry gets an explanation rather than an
          empty axis. */}
      <DeviceHistory deviceId={id} metrics={Object.keys(metrics)} />

      <p><Link to="/devices">← All devices</Link></p>
    </div>
  );
}

/** Poll totals for one endpoint, with the failure breakdown only when there is
 *  one. A bare "2547" reads as healthy at a glance; "2547 · 12 failed" does
 *  not, and that is the whole job of this cell.
 *
 *  Timeouts and auth failures are separated because they send an operator to
 *  different places: a timeout is a network or device problem, while auth
 *  failures mean the stored credential is wrong and no amount of waiting will
 *  fix it. */
function PollCounts({ endpoint }: { endpoint: EndpointSummary }) {
  const { poll_count: polls, fail_count: fails } = endpoint;
  if (!polls && !fails) return <span className="muted">—</span>;

  const parts: string[] = [];
  if (endpoint.timeout_count) parts.push(`${endpoint.timeout_count} timeout`);
  if (endpoint.auth_fail_count) parts.push(`${endpoint.auth_fail_count} auth`);
  // Failures that are neither: refused, decode, unreachable and the rest.
  const other = fails - endpoint.timeout_count - endpoint.auth_fail_count;
  if (other > 0) parts.push(`${other} other`);

  return (
    <>
      <span>{polls.toLocaleString()}</span>
      {fails > 0 && (
        <span className="warn" title={parts.join(', ')}>
          {' · '}{fails.toLocaleString()} failed
        </span>
      )}
    </>
  );
}
