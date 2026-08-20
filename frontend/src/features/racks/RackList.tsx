import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type RackSummary } from '../../api/client';
import { StatusChip } from '../../components/StatusChip';

export function RackList() {
  const q = useQuery<{ items: RackSummary[] }>({
    queryKey: ['racks'],
    queryFn: () => api.racks({ limit: '200' }),
    refetchInterval: 30_000,
  });

  if (q.isLoading) return <p className="muted">Loading…</p>;
  if (q.isError || !q.data) return <p className="warn">Could not load racks.</p>;

  const racks = q.data.items;

  return (
    <div className="stack">
      <h2>Racks</h2>
      <p className="muted">
        Load is the sum of what the devices in the rack are actually drawing,
        not a nameplate total. Free U counts rails, so a rack full of zero-U
        gear still reads as empty space.
      </p>
      <table>
        <thead>
          <tr>
            <th>Rack</th><th>Location</th>
            <th className="num">Devices</th><th className="num">Load</th>
            <th className="num">Free U</th><th className="num">Max inlet</th>
            <th>Alarms</th>
          </tr>
        </thead>
        <tbody>
          {racks.map((r) => (
            <tr key={r.id}>
              <td><Link to={`/racks/${r.id}`}>{r.name}</Link></td>
              <td className="muted">
                {[r.datacenter_code, r.room_name, r.row_name].filter(Boolean).join(' · ')}
              </td>
              <td className="num">
                {r.device_count}
                {r.offline_count > 0 && (
                  <span className="warn"> · {r.offline_count} off</span>
                )}
              </td>
              <td className="num">
                {r.load_kw != null ? `${r.load_kw.toFixed(1)} kW` : '—'}
                {r.load_pct != null && (
                  <span className="muted"> ({r.load_pct.toFixed(0)}%)</span>
                )}
              </td>
              <td className="num">{r.free_u ?? '—'}</td>
              <td className="num">
                {r.max_inlet_c != null ? `${r.max_inlet_c.toFixed(1)} °C` : '—'}
              </td>
              <td><StatusChip status={r.max_severity} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      {racks.length === 0 && <p className="muted">No racks.</p>}
    </div>
  );
}
