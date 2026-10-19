import { useState } from 'react';
import { type DeviceDetail as Detail, type EndpointSummary } from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { Tip } from '../../components/HoverTip';
import { EndpointEditor } from './EndpointEditor';
import { humanise, relativeTime } from '../../lib/format';

/** How the device is polled: every endpoint, who polls it, with what, and
 *  how that has been going. The Monitoring tab. */
export function DeviceEndpoints({ device }: { device: Detail }) {
  const [editing, setEditing] = useState<EndpointSummary | null>(null);
  const d = device;

  return (
    <section>
      <h3>Communication<span className="count"> {d.endpoints.length}</span></h3>
      {d.endpoints.length === 0 && (
        <p className="dv-empty">No endpoints configured for this device.</p>
      )}
      {d.endpoints.length > 0 && (
      <div className="table-frame" style={{ overflowX: 'auto' }}>
      <table>
        <thead>
          <tr>
            <th>Protocol</th><th>Role</th><th>Address</th><th>Selector</th>
            <th>Status</th><th>Credential</th><th className="num">Interval</th>
            {/* Seen and Success are not the same question: a fresh Seen beside
                a stale Success is an endpoint being polled and failing. */}
            <th><Tip tip="Last poll attempt">Seen</Tip></th>
            <th><Tip tip="Last poll that answered">Success</Tip></th>
            <th className="num">Latency</th>
            <th className="num">
              <Tip tip="Cumulative for the life of the endpoint, not a recent window">
                Polls
              </Tip>
            </th>
            <th>Last error</th><th />
          </tr>
        </thead>
        <tbody>
          {d.endpoints.map((e) => (
            <tr key={e.id}>
              <td>{humanise(e.protocol)}</td>
              <td className="muted">{humanise(e.role)}</td>
              <td className="mono">
                {e.address}{e.port ? `:${e.port}` : ''}
                {e.via_name && (
                  <Tip className="muted" tip="reached through a gateway">
                    {' '}via {e.via_name}
                  </Tip>
                )}
              </td>
              {/* What selects this device when the address alone does not:
                  a Modbus unit ID, a BACnet device instance. */}
              <td className="mono muted">{selector(e)}</td>
              <td>
                <StatusChip status={e.status} />
                {e.reported_by && (
                  <div className="muted">
                    monitored by <span className="mono">{e.reported_by}</span>
                    {e.pool_name ? ` (pool ${e.pool_name})` : ''}
                  </div>
                )}
                {/* admin_state is what an operator asked for; `enabled` is
                    whether the collector is given this endpoint at all. */}
                {!e.enabled ? (
                  <Tip className="muted"
                       tip="Retired: this device type does not serve this protocol, so the collector is not given it.">
                    {' · retired'}
                  </Tip>
                ) : e.admin_state !== 'enabled' && (
                  <span className="muted"> · {humanise(e.admin_state)}</span>
                )}
              </td>
              {/* Only ever the hint - the secret itself never leaves the server. */}
              <td className="muted mono">
                {e.credential_hint
                  ? <Tip tip={e.credential_hint}>{e.credential_name ?? '—'}</Tip>
                  : (e.credential_name ?? '—')}
              </td>
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
              <td>
                <button onClick={() => setEditing(e)}>Edit</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      )}
      {editing && (
        <EndpointEditor deviceId={d.id} endpoint={editing}
                        onClose={() => setEditing(null)} />
      )}
    </section>
  );
}

/** Poll totals for one endpoint, with the failure breakdown only when there is
 *  one. A bare "2547" reads as healthy at a glance; "2547 · 12 failed" does
 *  not, and that is the whole job of this cell. Timeouts and auth failures
 *  are separated because they send an operator to different places. */
function PollCounts({ endpoint }: { endpoint: EndpointSummary }) {
  const { poll_count: polls, fail_count: fails } = endpoint;
  if (!polls && !fails) return <span className="muted">—</span>;

  const parts: string[] = [];
  if (endpoint.timeout_count) parts.push(`${endpoint.timeout_count} timeout`);
  if (endpoint.auth_fail_count) parts.push(`${endpoint.auth_fail_count} auth`);
  const other = fails - endpoint.timeout_count - endpoint.auth_fail_count;
  if (other > 0) parts.push(`${other} other`);

  return (
    <>
      <span>{polls.toLocaleString()}</span>
      {fails > 0 && (
        <Tip className="warn" tip={parts.join(', ')}>
          {' · '}{fails.toLocaleString()} failed
        </Tip>
      )}
    </>
  );
}

/** The protocol-specific identifier that picks this device out, for a cell. */
function selector(e: EndpointSummary): string {
  const bits = Object.entries(e.addressing ?? {})
    .filter(([, v]) => v !== '' && v != null)
    .map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`);
  return bits.length ? bits.join(' · ') : '—';
}
