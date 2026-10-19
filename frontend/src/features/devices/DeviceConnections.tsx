import { Link } from 'react-router-dom';
import type { UseQueryResult } from '@tanstack/react-query';
import { type DeviceDetail as Detail, type NetworkInterface } from '../../api/client';
import { formatSpeed, humanise } from '../../lib/format';

/** Whether the supplies actually buy anything.
 *
 *  Two PSUs are redundancy only if they are fed from different sources. Two
 *  cords into one strip is a single point of failure wearing a pair of
 *  supplies, and it looks identical from a count. */
export function psuVerdict(d: Detail): { text: string; warn: boolean } | null {
  const psus = d.psus ?? [];
  if (psus.length === 0) return null;
  const feeds = new Set(psus.map((p) => p.feed_device_id).filter((x): x is string => Boolean(x)));
  const uncorded = psus.filter((p) => !p.feed_device_id).length;
  if (uncorded > 0) return { text: `${uncorded} not corded`, warn: true };
  if (psus.length === 1) return { text: 'single-corded', warn: true };
  if (feeds.size > 1) return { text: `${feeds.size} independent feeds`, warn: false };
  return { text: 'both cords on one PDU', warn: true };
}

/** "2 × 25 Gb/s data, 1 × 1 Gb/s management": the port inventory as one line.
 *  Grouped by role AND speed because a server with two 25G NICs and a 1G BMC
 *  is three different things on one chassis. */
export function portSummary(ports: NetworkInterface[] | undefined): string | null {
  if (!ports?.length) return null;
  const buckets = new Map<string, number>();
  for (const p of ports) {
    const k = `${formatSpeed(p.speed_bps)}|${humanise(p.role)}`;
    buckets.set(k, (buckets.get(k) ?? 0) + 1);
  }
  return [...buckets.entries()]
    .map(([k, n]) => { const [sp, role] = k.split('|'); return `${n} × ${sp} ${role}`; })
    .join(', ');
}

/** The Connections tab: what the device is corded and patched to. */
export function DeviceConnections({ device, ports }: {
  device: Detail;
  ports: UseQueryResult<NetworkInterface[]>;
}) {
  const d = device;
  const fitted = ports.data?.length ?? 0;
  const cabled = ports.data?.filter((p) => p.peer_device).length ?? 0;
  const verdict = psuVerdict(d);

  return (
    <>
      <section>
        <h3>
          Power supplies
          {d.psus.length > 0 && <span className="count"> {d.psus.length}</span>}
          {verdict && (
            <span className={`dv-verdict ${verdict.warn ? 'warn' : 'ok'}`} style={{ marginLeft: 'auto' }}>
              {verdict.text}
            </span>
          )}
        </h3>
        {d.psus.length === 0 ? (
          <p className="dv-empty">No power supplies recorded for this device.</p>
        ) : (
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
                        <span className="warn">not corded</span>
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
        <h3>
          Network ports
          {fitted > 0 && <span className="count"> {fitted}</span>}
          {fitted > 0 && (
            <span className="lead" style={{ marginLeft: 'auto' }}>
              {cabled} of {fitted} patched
            </span>
          )}
        </h3>
        {ports.isLoading && <p className="dv-empty">Loading…</p>}
        {ports.error && <p className="warn">Could not load ports.</p>}
        {ports.data && ports.data.length === 0 && (
          <p className="dv-empty">No ports recorded for this device.</p>
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
                    <td className="muted">{humanise(p.role)}</td>
                    <td>{formatSpeed(p.speed_bps)}</td>
                    <td className="mono muted">{p.mac ?? '—'}</td>
                    <td>
                      {p.peer_device_id ? (
                        <>
                          <Link to={`/devices/${p.peer_device_id}`}>{p.peer_device}</Link>
                          {p.peer_port && <span className="muted"> · {p.peer_port}</span>}
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
    </>
  );
}
