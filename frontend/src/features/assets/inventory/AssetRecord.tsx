import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import {
  api,
  type DeviceDetail,
  type NetworkInterface,
  type PowerChain,
} from '../../../api/client';
import { humanise } from '../../../lib/format';
import { LifecycleChip } from '../components/LifecycleChip';

/** One asset, as an asset.
 *
 *  Deliberately NOT DeviceDetail. /devices/:id is the operational view -
 *  telemetry, charts, alarms - and it does that well. This is the asset record:
 *  identity, placement, ownership and the physical chain. Different questions,
 *  different people, and neither page grows the other's tabs (docs/22 §5).
 *
 *  There is no telemetry tab here and no charts. The header links out instead.
 */

type Tab = 'overview' | 'placement' | 'connections';

const TABS: { id: Tab; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'placement', label: 'Placement' },
  { id: 'connections', label: 'Connections' },
];

function Fact({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="asset-fact">
      <div className="k">{k}</div>
      <div className="v">{v ?? <span className="asset-none">—</span>}</div>
    </div>
  );
}

export function AssetRecord() {
  const { id = '' } = useParams();
  const [tab, setTab] = useState<Tab>('overview');

  const { data, error, isLoading } = useQuery<DeviceDetail>({
    queryKey: ['device', id],
    queryFn: () => api.device(id),
    enabled: Boolean(id),
  });

  const { data: interfaces } = useQuery<NetworkInterface[]>({
    queryKey: ['device-interfaces', id],
    queryFn: () => api.interfaces(id),
    enabled: Boolean(id) && tab === 'connections',
  });

  const { data: chain } = useQuery<PowerChain>({
    queryKey: ['power-chain', id],
    queryFn: () => api.powerChain(id),
    enabled: Boolean(id) && tab === 'connections',
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (isLoading || !data) return <p className="muted">Loading…</p>;

  const loc = data.location;

  return (
    <>
      <p className="asset-table-note">
        <Link to="/assets/inventory">← Inventory</Link>
      </p>

      <div className="asset-record-head">
        <h2>{data.name}</h2>
        <span className="asset-tag">
          {data.asset_tag ?? <span className="asset-none">no asset tag</span>}
        </span>
        <LifecycleChip state={data.lifecycle} />
        {/* Out to the operational view. The reciprocal link on /devices/:id is
            deliberately NOT added - that page is out of scope. */}
        <Link to={`/devices/${data.id}`} style={{ marginLeft: 'auto' }}>
          Open in Devices →
        </Link>
      </div>

      <div className="asset-tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            className={tab === t.id ? 'active' : ''}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'overview' && (
        <div className="asset-facts">
          <Fact k="Asset tag" v={data.asset_tag} />
          <Fact k="Serial number" v={data.serial_number} />
          <Fact k="Type" v={humanise(data.device_type)} />
          <Fact k="Category" v={data.category ? humanise(data.category) : null} />
          <Fact k="Vendor" v={data.vendor} />
          <Fact k="Model" v={data.model} />
          <Fact k="Rated power" v={data.rated_power_w ? `${data.rated_power_w} W` : null} />
          <Fact k="Height" v={`${data.u_height}U`} />
          <Fact k="Lifecycle" v={humanise(data.lifecycle)} />
          <Fact k="Admin state" v={humanise(data.admin_state)} />
          <Fact k="Management IP" v={data.mgmt_ip} />
          <Fact k="Primary IP" v={data.primary_ip} />
        </div>
      )}

      {tab === 'placement' && (
        <div className="asset-facts">
          <Fact k="Site" v={loc.datacenter_code} />
          <Fact k="Room" v={loc.room_name} />
          <Fact k="Row" v={loc.row_name} />
          <Fact
            k="Rack"
            v={loc.rack_id
              ? <Link to={`/assets/estate/racks/${loc.rack_id}`}>{loc.rack_name}</Link>
              : null}
          />
          <Fact
            k="Position"
            v={loc.u_start ? `U${loc.u_start}–U${loc.u_start + data.u_height - 1}` : null}
          />
          <Fact k="Facing" v={data.facing} />
        </div>
      )}

      {tab === 'connections' && (
        <>
          <h3 style={{ marginTop: 0 }}>
            Power feeds{' '}
            {chain && (
              <span className="muted" style={{ fontWeight: 400, fontSize: '0.85rem' }}>
                — {humanise(chain.redundancy)}: {chain.reason}
              </span>
            )}
          </h3>
          {chain && chain.paths.length > 0 ? (
            <div className="asset-cols">
              {chain.paths.map((path, i) => (
                <div className="asset-panel" key={path.side ?? i}>
                  <h3>
                    {path.side ? `Side ${path.side}` : 'Feed'}
                    {!path.reaches_source && (
                      // A path that stops short is not the same as a short
                      // path: it means the trace ran out before a source, and
                      // reading it as "fed" would be wrong.
                      <span className="muted"> — does not reach a source</span>
                    )}
                  </h3>
                  <ol className="asset-chain">
                    {path.hops.map((hop) => (
                      <li key={hop.device_id}>
                        <Link to={`/assets/inventory/${hop.device_id}`}>{hop.name}</Link>
                        <span className="muted"> · {humanise(hop.device_type)}</span>
                        {hop.load_pct != null && (
                          <span className="muted"> · {hop.load_pct.toFixed(0)}%</span>
                        )}
                      </li>
                    ))}
                  </ol>
                </div>
              ))}
            </div>
          ) : (
            <p className="muted">No power chain recorded for this asset.</p>
          )}

          <h3>Network ports</h3>
          {interfaces && interfaces.length > 0 ? (
            <div className="asset-scroll">
              <table>
                <thead>
                  <tr><th>Port</th><th>Role</th><th>Speed</th><th>MAC</th><th>Peer</th></tr>
                </thead>
                <tbody>
                  {interfaces.map((n) => (
                    <tr key={n.id}>
                      <td>{n.name}</td>
                      <td className="muted">{humanise(n.role)}</td>
                      <td className="muted">
                        {n.speed_bps ? `${Math.round(n.speed_bps / 1e9)} Gb/s` : '—'}
                      </td>
                      <td className="asset-tag">{n.mac ?? '—'}</td>
                      <td className="muted">{n.peer_device ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="muted">No ports recorded for this asset.</p>
          )}
        </>
      )}
    </>
  );
}
