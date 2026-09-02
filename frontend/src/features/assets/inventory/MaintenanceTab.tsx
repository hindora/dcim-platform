import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  api,
  type MaintenanceRecord,
  type MaintenanceWindow,
} from '../../../api/client';
import { humanise, relativeTime } from '../../../lib/format';

/** Work planned on this asset, and work done to it.
 *
 *  Two different questions on one tab because an operator standing at the rack
 *  asks them together: is anyone else booked on this machine, and what was the
 *  last thing done to it.
 */
export function MaintenanceTab({ deviceId }: { deviceId: string }) {
  const { data: windows } = useQuery<{ items: MaintenanceWindow[] }>({
    queryKey: ['maintenance-windows', 'device', deviceId],
    queryFn: () => api.maintenanceWindows({ device_id: deviceId }),
  });

  const { data: records, isLoading } = useQuery<{ items: MaintenanceRecord[] }>({
    queryKey: ['maintenance-records', deviceId],
    queryFn: () => api.maintenanceRecords(deviceId),
  });

  const scheduled = (windows?.items ?? []).filter(
    (w) => w.status === 'scheduled' || w.status === 'active');

  return (
    <>
      <h3 style={{ marginTop: 0 }}>Windows</h3>
      {scheduled.length === 0 ? (
        <p className="muted">No maintenance scheduled on this asset.</p>
      ) : (
        <div className="asset-scroll">
          <table>
            <thead>
              <tr><th>Window</th><th>Status</th><th>Starts</th><th>Ends</th></tr>
            </thead>
            <tbody>
              {scheduled.map((w) => (
                <tr key={w.id}>
                  <td><Link to={`/assets/maintenance/${w.id}`}>{w.title}</Link></td>
                  <td>
                    <span className={`asset-life is-${w.status}`}>
                      {humanise(w.status)}
                    </span>
                  </td>
                  <td className="muted" title={w.starts_at}>
                    {relativeTime(w.starts_at)}
                  </td>
                  <td className="muted" title={w.ends_at}>
                    {relativeTime(w.ends_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3>Work done</h3>
      {isLoading && <p className="muted">Loading…</p>}
      {!isLoading && (records?.items.length ?? 0) === 0 && (
        <p className="muted">
          Nothing recorded. A record is written when somebody completes work,
          not when a window closes — a window can end with nothing done.
        </p>
      )}
      {records && records.items.length > 0 && (
        <div className="asset-scroll">
          <table>
            <thead>
              <tr><th>When</th><th>Kind</th><th>Summary</th><th>By</th><th>Window</th></tr>
            </thead>
            <tbody>
              {records.items.map((r) => (
                <tr key={r.id}>
                  <td className="muted" title={r.performed_at}>
                    {relativeTime(r.performed_at)}
                  </td>
                  <td className="muted">{humanise(r.kind)}</td>
                  <td>{r.summary}</td>
                  <td className="muted">{r.performed_by}</td>
                  <td className="muted">
                    {r.window_id
                      ? <Link to={`/assets/maintenance/${r.window_id}`}>
                          {r.window_title ?? 'window'}
                        </Link>
                      : <span className="asset-none">unplanned</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
