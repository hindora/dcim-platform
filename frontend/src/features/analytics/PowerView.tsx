import { useQuery } from '@tanstack/react-query';
import { humanise } from '../../lib/format';
import { api, type PowerFleet, type RoomSummary } from '../../api/client';

/** Who has a second feed, and who only looks like they do.
 *
 *  A load is dual-fed when a path from a SOURCE reaches it without passing
 *  through the failed element - not merely when it has two cords. Two cords
 *  into the same RPP is a single feed wearing a disguise, and that is the
 *  distinction the census is built on.
 *
 *  The census is shown as a proportion of the fleet rather than as three
 *  numbers, because "234 single-feed" means nothing without the 654 it is out
 *  of.
 */

const TONE: Record<string, string> = {
  'N+1': 'ok',
  single_feed: 'warn',
  no_feed: 'critical',
};

export function PowerView({ room }: { room: RoomSummary }) {
  const dc = room.datacenter_id ?? undefined;
  const { data, error, isLoading } = useQuery<PowerFleet>({
    queryKey: ['power-fleet', dc],
    queryFn: () => api.powerFleet(dc),
    refetchInterval: 60_000,
  });

  if (isLoading) return <p className="muted">Loading…</p>;
  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (!data) return null;

  const census = data.redundancy_census ?? {};
  const total = Object.values(census).reduce((a, b) => a + b, 0);

  return (
    <>
      <h3>Redundancy census</h3>
      {total === 0 ? (
        <p className="muted">No powered devices in scope.</p>
      ) : (
        <>
          <div className="stack-bar">
            {Object.entries(census).map(([k, v]) => (
              <div key={k} className={`stack-seg ${TONE[k] ?? 'unknown'}`}
                   style={{ width: `${(v / total) * 100}%` }}
                   title={`${k}: ${v}`} />
            ))}
          </div>
          <div className="legend">
            {Object.entries(census).map(([k, v]) => (
              <span key={k}>
                <i className={TONE[k] ?? 'unknown'} />
                {k.replace(/_/g, ' ')} — {v} ({((v / total) * 100).toFixed(0)}%)
              </span>
            ))}
          </div>
        </>
      )}

      <p className="muted small">
        Dual-fed means a path from a source reaches the load without passing
        through the failed element. Two cords into the same RPP is a single feed.
      </p>

      {data.at_risk.length > 0 && (
        <>
          {/* Not "loads with one feed": the list also carries no_feed, which
              is a worse state, and a heading that undersells it is the kind of
              thing someone skims past. */}
          <h3>
            Loads without full redundancy
            {data.at_risk_total > data.at_risk.length &&
              ` (${data.at_risk.length} of ${data.at_risk_total})`}
          </h3>
          <table>
            <thead>
              <tr><th>Device</th><th>Type</th><th>Redundancy</th><th>Why</th></tr>
            </thead>
            <tbody>
              {data.at_risk.map((d) => (
                <tr key={d.device_id}>
                  <td>{d.name}</td>
                  <td className="muted">{humanise(d.device_type)}</td>
                  <td>
                    <span className={`chip ${TONE[d.redundancy] ?? 'unknown'}`}>
                      {d.redundancy.replace(/_/g, ' ')}
                    </span>
                  </td>
                  <td className="muted small">{d.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {data.supplies.length > 0 && (
        <>
          <h3>Supplies</h3>
          <table>
            <thead>
              <tr><th>Supply</th><th>Type</th><th>Status</th><th>Load</th><th>Source</th></tr>
            </thead>
            <tbody>
              {data.supplies.slice(0, 25).map((s) => (
                <tr key={s.device_id}>
                  <td>{s.name}</td>
                  <td className="muted">{humanise(s.device_type)}</td>
                  <td><span className={`chip ${s.status === 'ONLINE' ? 'ok' : 'warn'}`}>
                    {s.status}
                  </span></td>
                  <td className="num">
                    {s.load_w !== null ? `${(s.load_w / 1000).toFixed(1)} kW` : '—'}
                    {s.load_pct !== null && ` (${s.load_pct.toFixed(0)}%)`}
                  </td>
                  {/* "derived" means the load was summed from what hangs off it
                      rather than read from the device, which is a weaker claim
                      than a metered reading and is labelled as one. */}
                  <td className="muted small">{s.load_source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {data.phase_imbalance.length > 0 && (
        <>
          <h3>Phase imbalance</h3>
          <table>
            <thead><tr><th>Device</th><th>Imbalance</th></tr></thead>
            <tbody>
              {data.phase_imbalance.map((p) => (
                <tr key={p.device_id}>
                  <td>{p.name}</td>
                  <td className="num">{p.imbalance_pct.toFixed(1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}
