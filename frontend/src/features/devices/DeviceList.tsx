import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { api, type DeviceSummary, type Page } from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { humanise, relativeTime } from '../../lib/format';

/** The filters this page understands, and the query-string key each one uses.
 *
 *  They live in the URL rather than in component state so a link can arrive
 *  pre-filtered. That is not a nicety: ENTER on a site row and on the site
 *  drawer have linked to `/devices?datacenter=DC1` since they shipped, this
 *  page held its filters in useState, and the parameter was silently dropped -
 *  so every one of those links landed on an unfiltered list of the whole
 *  estate and looked like it had worked.
 */
const FILTERS = ['datacenter', 'room', 'type', 'category', 'status', 'q'] as const;

export function DeviceList() {
  const [params, setParams] = useSearchParams();
  const get = (k: string) => params.get(k) ?? '';
  const search = get('q');
  const deviceType = get('type');
  const status = get('status');
  const datacenter = get('datacenter');
  const roomId = get('room');
  // The room drawer's 'cooling units' and 'power units' tiles count by
  // device_type.category, not by a single device_type - a power unit is a
  // PDU or an RPP or a UPS. Linking on type would land on a SUBSET of the
  // figure the reader just clicked, which is worse than not linking.
  const category = get('category');

  const set = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v); else next.delete(k);
    setParams(next, { replace: true });
  };

  const { data, error, isLoading } = useQuery<Page<DeviceSummary>>({
    queryKey: ['devices', search, deviceType, status, datacenter, roomId, category],
    queryFn: () => api.devices({
      search: search || undefined,
      device_type: deviceType || undefined,
      status: status || undefined,
      datacenter: datacenter || undefined,
      room_id: roomId || undefined,
      category: category || undefined,
      limit: '100',
    }),
    refetchInterval: 15_000,
  });

  // Scope the reader arrived with, stated and reversible. A pre-filtered list
  // that does not say it is filtered is how somebody concludes the estate has
  // 145 devices in it.
  const scoped = FILTERS.filter((k) => params.get(k));

  return (
    <>
      <h2>Devices</h2>
      <p className="subtitle">
        Inventory seeded from the topology export; state from the collector.
      </p>

      {scoped.length > 0 && (
        <div className="scope-strip">
          <span className="muted">Showing</span>
          {datacenter && <span className="chip">site {datacenter}</span>}
          {roomId && <span className="chip">one room</span>}
          {deviceType && <span className="chip">{humanise(deviceType)}</span>}
          {category && <span className="chip">{category} equipment</span>}
          {status && <span className="chip">{status.toLowerCase()}</span>}
          {search && <span className="chip">“{search}”</span>}
          <button className="link-button" onClick={() => setParams({}, { replace: true })}>
            clear
          </button>
        </div>
      )}

      <div className="toolbar">
        <input
          placeholder="Search name, IP or serial"
          value={search}
          onChange={(e) => set('q', e.target.value)}
          style={{ minWidth: 240 }}
        />
        <select value={deviceType} onChange={(e) => set('type', e.target.value)}>
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
        <select value={status} onChange={(e) => set('status', e.target.value)}>
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
                  <td className="muted">{humanise(d.device_type)}</td>
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
