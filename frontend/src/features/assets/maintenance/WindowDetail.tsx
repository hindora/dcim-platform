import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api, type MaintenanceWindow } from '../../../api/client';
import { humanise, relativeTime } from '../../../lib/format';

/** One window: what it covers, and what it is holding back.
 *
 *  The shelved list is the reason this page exists. "Did anything ELSE break
 *  while we were in there" is asked after every window, and it can only be
 *  answered because the alarms were raised and stored rather than suppressed at
 *  source.
 */
export function WindowDetail() {
  const { id = '' } = useParams();

  const { data, isLoading, error } = useQuery<MaintenanceWindow>({
    queryKey: ['maintenance-window', id],
    queryFn: () => api.maintenanceWindow(id),
    enabled: Boolean(id),
    refetchInterval: 30_000,
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (isLoading || !data) return <p className="muted">Loading…</p>;

  return (
    <>
      <p className="asset-table-note">
        <Link to="/assets/maintenance">← Maintenance</Link>
      </p>

      <div className="asset-record-head">
        <h2>{data.title}</h2>
        <span className={`asset-life is-${data.status}`}>{humanise(data.status)}</span>
      </div>

      <div className="asset-facts" style={{ marginBottom: 20 }}>
        <div className="asset-fact">
          <div className="k">Kind</div><div className="v">{humanise(data.kind)}</div>
        </div>
        <div className="asset-fact">
          <div className="k">Starts</div>
          <div className="v" title={data.starts_at}>{relativeTime(data.starts_at)}</div>
        </div>
        <div className="asset-fact">
          <div className="k">Ends</div>
          <div className="v" title={data.ends_at}>{relativeTime(data.ends_at)}</div>
        </div>
        <div className="asset-fact">
          <div className="k">Change reference</div>
          <div className="v asset-tag">
            {data.change_ref ?? <span className="asset-none">—</span>}
          </div>
        </div>
        <div className="asset-fact">
          <div className="k">Scheduled by</div><div className="v">{data.created_by}</div>
        </div>
        <div className="asset-fact">
          <div className="k">Suppressing</div>
          <div className="v">{data.suppress ? 'Yes' : 'No — calendar only'}</div>
        </div>
      </div>

      {data.description && <p className="muted">{data.description}</p>}

      <h3>Devices — {data.targets?.length ?? 0}</h3>
      {data.targets && data.targets.length > 0 ? (
        <div className="asset-scroll">
          <table>
            <thead><tr><th>Device</th><th>Type</th><th>Severity</th></tr></thead>
            <tbody>
              {data.targets.map((t) => (
                <tr key={t.id}>
                  <td><Link to={`/assets/inventory/${t.id}`}>{t.name}</Link></td>
                  <td className="muted">{humanise(t.device_type)}</td>
                  <td className="muted">{t.max_severity}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">This window covers no devices, so it silences nothing.</p>
      )}

      <h3 style={{ marginTop: 24 }}>
        Shelved alarms — {data.shelved?.length ?? 0}
      </h3>
      <p className="muted">
        Raised and recorded as normal, held out of the active list and the
        roll-ups while this window runs. Anything still open when it ends comes
        back automatically.
      </p>
      {data.shelved && data.shelved.length > 0 ? (
        <div className="asset-scroll">
          <table>
            <thead>
              <tr>
                <th>Device</th><th>Alarm</th><th>Severity</th>
                <th>State</th><th>Since</th>
              </tr>
            </thead>
            <tbody>
              {data.shelved.map((a) => (
                <tr key={a.id}>
                  <td><Link to={`/assets/inventory/${a.device_id}`}>{a.device_name}</Link></td>
                  <td>{humanise(a.alarm_type)}</td>
                  <td className="muted">{a.severity}</td>
                  <td className="muted">{humanise(a.state)}</td>
                  <td className="muted" title={a.first_seen}>
                    {relativeTime(a.first_seen)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">Nothing shelved.</p>
      )}
    </>
  );
}
