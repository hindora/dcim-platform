import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type MaintenanceWindow } from '../../../api/client';
import { humanise, relativeTime } from '../../../lib/format';

/** Maintenance windows, and what each is holding out of the alarm console.
 *
 *  `shelved_alarms` is the column worth reading. A window shelving far more
 *  than the work would explain was scoped too widely, and this is where that
 *  shows before somebody discovers it as a missed outage.
 */
export function WindowList() {
  const { data, isLoading, error } = useQuery<{ items: MaintenanceWindow[] }>({
    queryKey: ['maintenance-windows'],
    queryFn: () => api.maintenanceWindows({ limit: '200' }),
    refetchInterval: 30_000,
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;

  const items = data?.items ?? [];
  const active = items.filter((w) => w.status === 'active');
  const upcoming = items.filter((w) => w.status === 'scheduled');
  const past = items.filter((w) => w.status === 'completed' || w.status === 'cancelled');

  return (
    <>
      <h2>Maintenance</h2>
      <p className="subtitle">
        Planned work, and the alarms it is holding back. An alarm raised on a
        device inside a running window is still recorded — it is kept out of the
        active list, not thrown away.
      </p>

      {isLoading && <p className="muted">Loading…</p>}

      {!isLoading && items.length === 0 && (
        <div className="asset-empty">
          No maintenance windows. Scheduling one silences alarms on the devices
          it covers for its duration, so planned work does not page anyone.
        </div>
      )}

      {active.length > 0 && <Section title="Running now" rows={active} />}
      {upcoming.length > 0 && <Section title="Scheduled" rows={upcoming} />}
      {past.length > 0 && <Section title="Finished" rows={past} />}
    </>
  );
}

function Section({ title, rows }: { title: string; rows: MaintenanceWindow[] }) {
  return (
    <section style={{ marginBottom: 24 }}>
      <h3>{title} — {rows.length}</h3>
      <div className="asset-scroll">
        <table>
          <thead>
            <tr>
              <th>Window</th><th>Kind</th><th>Starts</th><th>Ends</th>
              <th>Devices</th><th>Shelved</th><th>Change</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((w) => (
              <tr key={w.id}>
                <td>
                  <Link to={`/assets/maintenance/${w.id}`}>{w.title}</Link>
                  {!w.suppress && (
                    // A window that silences nothing is a calendar entry. Saying
                    // so stops somebody assuming they are covered.
                    <span className="muted"> · not suppressing</span>
                  )}
                </td>
                <td className="muted">{humanise(w.kind)}</td>
                <td className="muted" title={w.starts_at}>{relativeTime(w.starts_at)}</td>
                <td className="muted" title={w.ends_at}>{relativeTime(w.ends_at)}</td>
                <td className="muted">{w.target_count}</td>
                <td className={w.shelved_alarms ? 'asset-shelved' : 'muted'}>
                  {w.shelved_alarms || '—'}
                </td>
                <td className="asset-tag">
                  {w.change_ref ?? <span className="asset-none">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
