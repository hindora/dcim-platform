import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import {
  api,
  type DeviceDetail as Detail,
  type DeviceState,
  type EndpointSummary,
  type NetworkInterface,
} from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { DeviceHistory, type ChartGroup } from './DeviceHistory';
import { EndpointEditor } from './EndpointEditor';
import { formatMetric, formatSpeed, metricLabel, relativeTime } from '../../lib/format';

/** What an operator opens a server page to ask, in the order they ask it.
 *
 * Each panel is one unit, so each frame has ONE axis. Putting watts and
 * degrees on a shared frame with two scales lets any two series be made to
 * look correlated by choosing the ranges - the most common way a chart lies -
 * and it is why these are three panels rather than one.
 *
 * Draw sits first because it is the question the rack and the feed are sized
 * against. Then the thermal pair: intake is what the room delivers and the CPU
 * is what the machine does with it, so the GAP between them is the reading -
 * a CPU climbing on a flat intake is the server's own problem, and both
 * climbing together is the room's. Utilisation is last: it explains the other
 * two rather than standing alone.
 */
const SERVER_CHARTS: ChartGroup[] = [
  { title: 'Power draw', metrics: ['power_draw'] },
  {
    title: 'CPU and intake temperature',
    metrics: ['cpu_temperature', 'inlet_temperature'],
    caption: 'Intake is what the room delivers; the gap to the CPU is what the machine adds.',
  },
  {
    title: 'Utilisation',
    metrics: ['cpu_utilization', 'memory_utilization', 'disk_utilization'],
  },
];

export function DeviceDetail() {
  const { id = '' } = useParams();
  const [editing, setEditing] = useState<EndpointSummary | null>(null);

  const device = useQuery<Detail>({
    queryKey: ['device', id],
    queryFn: () => api.device(id),
    enabled: Boolean(id),
  });

  // Ports are inventory: they change when somebody re-cables a rack, not on a
  // poll interval, so this is fetched once rather than refreshed like state.
  const ports = useQuery<NetworkInterface[]>({
    queryKey: ['device-interfaces', id],
    queryFn: () => api.interfaces(id),
    enabled: Boolean(id),
    retry: false,
  });

  const state = useQuery<DeviceState>({
    queryKey: ['device-state', id],
    queryFn: () => api.deviceState(id),
    enabled: Boolean(id),
    // 30s, not 10s. The slowest source behind this table is a 120s BMC poll and
    // the fastest a 10s sensor, so a 10s refetch spent up to twelve requests per
    // new reading. This still beats every source to the punch without asking the
    // API for numbers that cannot have changed.
    refetchInterval: 30_000,
    // A device with no telemetry yet is a normal state, not an error.
    retry: false,
  });

  if (device.isLoading) return <p className="muted">Loading…</p>;
  if (device.error) return <div className="banner">Failed to load: {String(device.error)}</div>;
  if (!device.data) return null;

  const d = device.data;
  const metrics = state.data?.metrics ?? {};
  // A curated page asks for exactly the keys its panels plot, rather than
  // everything the device happens to report - and it asks for them even when
  // none have arrived, so an absent metric can be named as absent instead of
  // silently missing from a chart that still looks complete.
  /** Whether the supplies actually buy anything.
   *
   *  Two PSUs are redundancy only if they are fed from different sources. Two
   *  cords into one strip is a single point of failure wearing a pair of
   *  supplies, and it looks identical from a count - which is what this said
   *  before: `psus.length > 1` labelled "redundant". True across this estate
   *  today, and it would have gone on saying so the moment somebody re-cabled
   *  both cords to one PDU, which is precisely the state worth flagging.
   */
  const feeds = new Set((d.psus ?? []).map((p) => p.feed_device_id)
                                      .filter((x): x is string => Boolean(x)));
  const uncorded = (d.psus ?? []).filter((p) => !p.feed_device_id).length;
  const psuVerdict = d.psus.length === 0 ? null
    : uncorded > 0 ? { text: `${uncorded} not corded`, warn: true }
    : d.psus.length === 1 ? { text: 'single-corded', warn: true }
    : feeds.size > 1 ? { text: `${feeds.size} independent feeds`, warn: false }
    : { text: 'both cords on one PDU', warn: true };

  // "4 x 1 Gb/s data, 1 x 1 Gb/s mgmt" - the port inventory as one line, which
  // is the nameplate fact. Grouped by role AND speed because a server with two
  // 25G NICs and a 1G BMC is three different things on one chassis, and a bare
  // count of five would say none of it.
  const fitted = ports.data?.length ?? 0;
  const cabled = ports.data?.filter((p) => p.peer_device).length ?? null;
  const portSummary = (() => {
    if (!ports.data?.length) return null;
    const buckets = new Map<string, number>();
    for (const p of ports.data) {
      const k = `${formatSpeed(p.speed_bps)}|${p.role}`;
      buckets.set(k, (buckets.get(k) ?? 0) + 1);
    }
    return [...buckets.entries()]
      .map(([k, n]) => { const [sp, role] = k.split('|'); return `${n} × ${sp} ${role}`; })
      .join(', ');
  })();

  // The live draw, for the headroom line beside the rating. Only meaningful
  // next to a nameplate, which is why it is read here and not in the metrics
  // table - that one shows every reading without ranking them.
  const draw = typeof metrics.power_draw?.v === 'number'
    ? metrics.power_draw.v
    : null;
  const chartMetrics = d.device_type === 'server'
    ? [...new Set(SERVER_CHARTS.flatMap((g) => g.metrics))]
    : Object.keys(metrics);

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
        <h3>Nameplate</h3>
        <dl className="kv">
          <dt>Rated power</dt>
          <dd>
            {d.rated_power_w != null ? `${d.rated_power_w} W` : '—'}
            {d.rated_power_w != null && draw != null && (
              <span className="muted">
                {' · '}drawing {Math.round(draw)} W
                {' '}({Math.round((draw / d.rated_power_w) * 100)}% of nameplate)
              </span>
            )}
          </dd>
          <dt>Power supplies</dt>
          <dd>
            {d.psus.length === 0 ? '—' : (
              <>
                {d.psus.length} × {d.psus[0].rated_watts ?? '?'} W
                {' '}({[...new Set(d.psus.map((p) => p.connector ?? '?'))].join(', ')})
                {/* The verdict, not the count: supplies are only redundancy if
                    something separate feeds each one. */}
                {psuVerdict && (
                  <span className={psuVerdict.warn ? 'warn' : 'muted'}>
                    {' · '}{psuVerdict.text}
                  </span>
                )}
              </>
            )}
          </dd>
          <dt>Network ports</dt>
          <dd>
            {portSummary ?? '—'}
            {/* Patched vs fitted. A rack with no spare ports is a constraint on
                the next install, and it is invisible from a port count alone. */}
            {cabled != null && fitted > 0 && (
              <span className="muted">
                {' · '}{cabled} of {fitted} patched
              </span>
            )}
          </dd>
          <dt>Rack units</dt>
          <dd>
            {d.u_height}U
            {d.location.u_start ? ` at U${d.location.u_start}` : ''}
          </dd>
          <dt>Serial</dt><dd className="mono">{d.serial_number || '—'}</dd>
          <dt>Asset tag</dt><dd className="mono">{d.asset_tag || '—'}</dd>
        </dl>
      </section>

      {d.psus.length > 0 && (
        <section>
          <h3>Power supplies<span className="count"> {d.psus.length}</span></h3>
          {/* Framed like every other table here, but not scrollable: a chassis
              has one to four supplies, and capping four rows would add a
              scrollbar to a list that was never going to need one. */}
          <div className="table-frame">
          <table>
            <thead>
              <tr>
                <th>Slot</th><th>Inlet</th><th className="num">Rated</th>
                <th>Fed from</th>
              </tr>
            </thead>
            <tbody>
              {d.psus.map((p) => (
                <tr key={p.number}>
                  <td>PSU{p.number}</td>
                  <td className="muted">{p.connector ?? '—'}</td>
                  <td className="num">
                    {p.rated_watts != null ? `${p.rated_watts} W` : '—'}
                  </td>
                  <td>
                    {p.feed_device_id ? (
                      <>
                        <Link to={`/devices/${p.feed_device_id}`}>{p.feed_device}</Link>
                        {p.feed_outlet != null && (
                          <span className="muted"> · outlet {p.feed_outlet}</span>
                        )}
                      </>
                    ) : (
                      /* Fitted but not corded. A blank would read as missing
                         data; this is a real state and a finding. */
                      <span className="warn">not corded</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </section>
      )}

      <section>
        <h3>
          Network ports
          {/* The count belongs in the heading once the table can scroll: with
              only a dozen rows visible, nothing else says whether this is a
              five-port server or a sixty-five-port switch. */}
          {fitted > 0 && <span className="count"> {fitted}</span>}
        </h3>
        {ports.isLoading && <p className="muted">Loading…</p>}
        {ports.error && <p className="warn">Could not load ports.</p>}
        {ports.data && ports.data.length === 0 && (
          <p className="muted">No ports recorded for this device.</p>
        )}
        {ports.data && ports.data.length > 0 && (
          <div className="table-frame is-scrollable">
          <table>
            <thead>
              <tr>
                <th>Port</th><th>Role</th><th>Speed</th><th>MAC</th>
                <th>Cabled to</th>
              </tr>
            </thead>
            <tbody>
              {ports.data.map((p) => (
                <tr key={p.id}>
                  <td>{p.name}</td>
                  <td className="muted">{p.role}</td>
                  <td>{formatSpeed(p.speed_bps)}</td>
                  <td className="mono muted">{p.mac ?? '—'}</td>
                  <td>
                    {p.peer_device_id ? (
                      <>
                        <Link to={`/devices/${p.peer_device_id}`}>{p.peer_device}</Link>
                        {p.peer_port && <span className="muted"> · {p.peer_port}</span>}
                        {/* Which plane the cable belongs to. A BMC on the
                            management fabric and a NIC on production fail
                            separately, and the row should say which is which. */}
                        {p.peer_layer && (
                          <span className="muted small"> · {p.peer_layer}</span>
                        )}
                      </>
                    ) : (
                      <span className="muted">not patched</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </section>

      <section>
        <h3>Communication</h3>
        {/* Rendered only when it has rows. It used to draw unconditionally with
            the "none configured" note underneath, which framed reads as an empty
            box with a header floating in it. */}
        {d.endpoints.length > 0 && (
        <div className="table-frame">
        <table>
          <thead>
            <tr>
              <th>Protocol</th><th>Role</th><th>Address</th><th>Selector</th>
              <th>Status</th><th>Credential</th><th className="num">Interval</th>
              {/* Seen and Success are not the same question, and the column
                  names alone do not say so: a fresh Seen beside a stale Success
                  is an endpoint being polled and failing, which is a different
                  fault from one nothing polls at all. Said where the confusion
                  happens rather than in a paragraph above the table. */}
              <th title="Last poll attempt">Seen</th>
              <th title="Last poll that answered">Success</th>
              <th className="num">Latency</th>
              <th className="num" title="Cumulative for the life of the endpoint, not a recent window">
                Polls
              </th>
              <th>Last error</th><th />
            </tr>
          </thead>
          <tbody>
            {d.endpoints.map((e) => (
              <tr key={e.id}>
                <td>{e.protocol}</td>
                <td className="muted">{e.role}</td>
                <td className="mono">
                  {e.address}{e.port ? `:${e.port}` : ''}
                  {e.via_name && (
                    <span className="muted" title="reached through a gateway">
                      {' '}via {e.via_name}
                    </span>
                  )}
                </td>
                {/* What selects this device when the address alone does not:
                    a Modbus unit ID, a BACnet device instance. Empty for gear
                    that answers on its own IP. */}
                <td className="mono muted">{selector(e)}</td>
                <td>
                  <StatusChip status={e.status} />
                  {/* Two different "off" states, and they answer different
                      questions. admin_state is what an operator asked for;
                      `enabled` is whether the collector is given this endpoint
                      at all, which the importer turns off when a device type
                      turns out not to speak the protocol - a firewall has no
                      gNMI to poll however anyone feels about it. Showing only
                      admin_state left 52 retired endpoints reading as
                      "enabled" while nothing polled them. */}
                  {!e.enabled ? (
                    <span className="muted"
                          title="Retired: this device type does not serve this
                                 protocol, so the collector is not given it.">
                      {' · retired'}
                    </span>
                  ) : e.admin_state !== 'enabled' && (
                    <span className="muted"> · {e.admin_state}</span>
                  )}
                </td>
                {/* Only ever the hint - the secret itself never leaves the server. */}
                <td className="muted mono" title={e.credential_hint ?? undefined}>
                  {e.credential_name ?? '—'}
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
        {d.endpoints.length === 0 && (
          <p className="muted">No endpoints configured for this device.</p>
        )}
        {editing && (
          <EndpointEditor deviceId={id} endpoint={editing}
                          onClose={() => setEditing(null)} />
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
          <div className="table-frame">
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
          </div>
        )}
      </section>

      {/* Charted from the same metric keys the device is actually reporting,
          so a device with no telemetry gets an explanation rather than an
          empty axis. Servers get curated panels; everything else still
          groups by unit, which is a fallback rather than a layout. */}
      <DeviceHistory deviceId={id} metrics={chartMetrics}
                     groups={d.device_type === 'server' ? SERVER_CHARTS : undefined} />

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

/** The protocol-specific identifier that picks this device out, rendered for a
 *  table cell. A Modbus slave behind a gateway and a BACnet device on a routed
 *  trunk are both reached at somebody else's address; this column is where an
 *  operator sees which one they actually are. */
function selector(e: EndpointSummary): string {
  const bits = Object.entries(e.addressing ?? {})
    .filter(([, v]) => v !== '' && v != null)
    .map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`);
  return bits.length ? bits.join(' · ') : '—';
}
