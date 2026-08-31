import { useQuery } from '@tanstack/react-query';
import { api, type DashboardSummary } from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { formatValue, relativeTime } from '../../lib/format';

function Tile({ label, value, detail }: {
  label: string; value: string; detail?: string;
}) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {detail && <div className="detail">{detail}</div>}
    </div>
  );
}

function num(v: number | null | undefined, unit: string): string {
  return v === null || v === undefined ? '—' : formatValue(unit, v);
}

export function Dashboard() {
  const { data, error, isLoading } = useQuery<DashboardSummary>({
    queryKey: ['dashboard'],
    queryFn: api.dashboard,
    // Phase 1 polls; phase 2 replaces this with the WebSocket delta stream.
    refetchInterval: 10_000,
  });

  if (isLoading) return <p className="muted">Loading…</p>;
  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (!data) return null;

  const lag = data.ingest?.lag_seconds;
  // A dashboard that silently shows five-minute-old numbers during a collector
  // outage is worse than one that says so.

  return (
    <>
      <h2>Dashboard</h2>
      <p className="subtitle">
        As of {new Date(data.as_of).toLocaleTimeString()}
        {lag !== null && lag !== undefined && ` · ingest lag ${Math.round(lag)}s`}
      </p>

      <div className="tiles">
        <Tile label="Devices" value={String(data.devices.total)}
              detail={`${data.devices.online} online · ${data.devices.offline} offline`} />
        <Tile label="Degraded" value={String(data.devices.degraded)}
              detail={`${data.devices.unknown} unknown`} />
        <Tile label="IT Load" value={num(data.power?.it_load_kw, 'kW')}
              detail={`${data.power?.reporting_devices ?? 0} devices reporting`} />
        <Tile label="Cooling Load" value={num(data.power?.cooling_load_kw, 'kW')} />
        <Tile label="Avg Inlet" value={num(data.environment?.avg_inlet_c, 'C')}
              detail={`max ${num(data.environment?.max_inlet_c, 'C')}`} />
        <Tile label="Hot Spots" value={String(data.environment?.hot_spots ?? 0)}
              detail="inlet above 27 °C" />
      </div>

      <h3 style={{ marginTop: 28 }}>Collectors</h3>
      {data.collectors.length === 0 ? (
        <p className="muted">
          No collector has checked in yet. Start one and it will appear here.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Collector</th><th>Status</th><th>Version</th>
              <th className="num">Endpoints</th><th className="num">Online</th>
              <th>Last heartbeat</th>
            </tr>
          </thead>
          <tbody>
            {data.collectors.map((c) => (
              <tr key={c.id}>
                <td className="mono">{c.id}</td>
                <td><StatusChip status={c.status === 'HEALTHY' ? 'ONLINE' : c.status} /></td>
                <td className="muted">{c.version ?? '—'}</td>
                <td className="num">{c.endpoints_owned}</td>
                <td className="num">{c.endpoints_online}</td>
                <td className="muted">{relativeTime(c.last_heartbeat)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
