import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api, type DeviceSummary, type Page } from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { relativeTime } from '../../lib/format';

export function DeviceList() {
  const [search, setSearch] = useState('');
  const [deviceType, setDeviceType] = useState('');
  const [status, setStatus] = useState('');

  const { data, error, isLoading } = useQuery<Page<DeviceSummary>>({
    queryKey: ['devices', search, deviceType, status],
    queryFn: () => api.devices({
      search: search || undefined,
      device_type: deviceType || undefined,
      status: status || undefined,
      limit: '100',
    }),
    refetchInterval: 15_000,
  });

  return (
    <>
      <h2>Devices</h2>
      <p className="subtitle">
        Inventory seeded from the topology export; state from the collector.
      </p>

      <div className="toolbar">
        <input
          placeholder="Search name, IP or serial"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ minWidth: 240 }}
        />
        <select value={deviceType} onChange={(e) => setDeviceType(e.target.value)}>
          <option value="">All types</option>
          <option value="server">Server</option>
          <option value="switch">Switch</option>
          <option value="router">Router</option>
          <option value="firewall">Firewall</option>
          <option value="oob_switch">OOB switch</option>
          <option value="pdu">Rack PDU</option>
          <option value="ups">UPS</option>
          <option value="crah">CRAH</option>
          <option value="cdu">CDU</option>
          <option value="sensor">Sensor</option>
        </select>
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">Any status</option>
          <option value="ONLINE">Online</option>
          <option value="DEGRADED">Degraded</option>
          <option value="OFFLINE">Offline</option>
          <option value="UNKNOWN">Unknown</option>
        </select>
      </div>

      {isLoading && <p className="muted">Loading…</p>}
      {error && <div className="banner">Failed to load: {String(error)}</div>}

      {data && (
        <>
          <table>
            <thead>
              <tr>
                <th>Name</th><th>Type</th><th>Status</th>
                <th>Management IP</th><th>Location</th><th>Last seen</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((d) => (
                <tr key={d.id}>
                  <td><Link to={`/devices/${d.id}`}>{d.name}</Link></td>
                  <td className="muted">{d.device_type}</td>
                  <td><StatusChip status={d.status} /></td>
                  <td className="mono">{d.mgmt_ip ?? d.primary_ip ?? '—'}</td>
                  <td className="muted">
                    {[d.location.datacenter_code, d.location.room_name,
                      d.location.rack_name,
                      d.location.u_start ? `U${d.location.u_start}` : null]
                      .filter(Boolean).join(' · ') || '—'}
                  </td>
                  <td className="muted">{relativeTime(d.last_seen)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {data.items.length === 0 && (
            <p className="muted">
              No devices match. If the inventory is empty, run the seed importer.
            </p>
          )}
          {data.next_cursor && (
            <p className="muted" style={{ marginTop: 12 }}>
              More results available — narrow the filters.
            </p>
          )}
        </>
      )}
    </>
  );
}
