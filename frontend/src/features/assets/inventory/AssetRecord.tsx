import { useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import {
  api,
  atLeast,
  type DeviceDetail,
  type LifecycleHistory,
  type MaintenanceRecord,
  type MaintenanceWindow,
  type NetworkInterface,
  type PowerChain,
  type SupportContract,
} from '../../../api/client';
import { humanise, relativeTime } from '../../../lib/format';
import { LifecycleChip } from '../components/LifecycleChip';
import { LifecycleTab } from './LifecycleTab';
import { MaintenanceTab } from './MaintenanceTab';
import { SupportTab } from './SupportTab';
import { TagAssignForm } from './RecordWorkForm';
import { DeviceEditor } from '../../devices/DeviceEditor';
import '../../devices/devices.css';

/** One asset, as an asset.
 *
 *  Deliberately NOT DeviceDetail. /devices/:id is the operational view -
 *  telemetry, alarms, what to do right now - and it does that well. This is
 *  the asset record: what it is, where it is in its life, what covers it,
 *  what is planned and done to it, and what feeds it. Different questions,
 *  different people (docs/22 §5). The two pages link to each other and share
 *  one editor, so a name changed here is the same name there.
 *
 *  The head answers the asset manager's first four questions without a
 *  scroll: lifecycle state, cover, contracts, next planned work. The
 *  lifecycle track under it is where the asset stands in its life, and
 *  where it may go next, read in one glance. */

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'lifecycle', label: 'Lifecycle' },
  { id: 'support', label: 'Support' },
  { id: 'maintenance', label: 'Maintenance' },
  { id: 'connections', label: 'Connections' },
] as const;
type Tab = typeof TABS[number]['id'];
const isTab = (s: string | null): s is Tab => TABS.some((t) => t.id === s);

/** The life of an asset, in the order it is lived. Maintenance is a side
 *  room off in-service rather than a step, and the track says so. */
const LIFE = ['planned', 'in_stock', 'installed', 'in_service', 'maintenance',
              'decommissioned', 'retired'];

function Fact({ k, v, mono }: { k: string; v?: ReactNode; mono?: boolean }) {
  const empty = v == null || v === '';
  return (
    <div className="dv-fact">
      <div className="k">{k}</div>
      <div className={`v${mono ? ' mono' : ''}`}>{empty ? <span className="dv-none">—</span> : v}</div>
    </div>
  );
}

/** The make once: many model strings already carry it. */
function makeModel(d: DeviceDetail): string {
  const v = d.vendor ?? 'unknown vendor';
  const m = d.model ?? '';
  if (m && d.vendor && m.toLowerCase().startsWith(d.vendor.toLowerCase())) return m;
  return `${v} ${m}`.trim();
}

function coverDays(d: DeviceDetail): number | null {
  if (!d.warranty_expires) return null;
  return Math.round((new Date(d.warranty_expires).getTime() - Date.now()) / 86_400_000);
}

function coverSentence(d: DeviceDetail): string {
  const days = coverDays(d);
  if (days == null) return 'No cover recorded.';
  const when = new Date(d.warranty_expires!).toLocaleDateString(undefined, {
    day: 'numeric', month: 'long', year: 'numeric',
  });
  if (days < 0) return `Cover expired ${Math.abs(days)} days ago, on ${when}.`;
  return `Covered until ${when}, ${days} days from now.`;
}

export function AssetRecord() {
  const { id = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const raw = params.get('tab');
  const tab: Tab = isTab(raw) ? raw : 'overview';
  const goTab = (t: Tab) => {
    const next = new URLSearchParams(params);
    if (t === 'overview') next.delete('tab'); else next.set('tab', t);
    setParams(next, { replace: true });
  };
  const [editingTags, setEditingTags] = useState(false);
  const [editingDevice, setEditingDevice] = useState(false);

  const { data, error, isLoading } = useQuery<DeviceDetail>({
    queryKey: ['device', id],
    queryFn: () => api.device(id),
    enabled: Boolean(id),
  });

  // The head reads these on every visit, so they are fetched up front; the
  // tabs read the same keys from cache.
  const life = useQuery<LifecycleHistory>({
    queryKey: ['lifecycle', id], queryFn: () => api.lifecycleHistory(id), enabled: Boolean(id),
  });
  const contracts = useQuery<{ items: SupportContract[] }>({
    queryKey: ['device-contracts', id], queryFn: () => api.deviceContracts(id), enabled: Boolean(id),
  });
  const windows = useQuery<{ items: MaintenanceWindow[] }>({
    queryKey: ['maintenance-windows', 'device', id],
    queryFn: () => api.maintenanceWindows({ device_id: id }), enabled: Boolean(id),
  });
  const records = useQuery<{ items: MaintenanceRecord[] }>({
    queryKey: ['maintenance-records', id], queryFn: () => api.maintenanceRecords(id), enabled: Boolean(id),
  });
  const chain = useQuery<PowerChain>({
    queryKey: ['power-chain', id], queryFn: () => api.powerChain(id), enabled: Boolean(id), retry: false,
  });
  const interfaces = useQuery<NetworkInterface[]>({
    queryKey: ['device-interfaces', id], queryFn: () => api.interfaces(id),
    enabled: Boolean(id) && tab === 'connections', retry: false,
  });

  if (error) return <div className="banner">Failed to load: {String(error)}</div>;
  if (isLoading || !data) return <p className="muted">Loading…</p>;

  const d = data;
  const loc = d.location;
  const active = (contracts.data?.items ?? []).filter((c) => c.state === 'active');
  const upcoming = (windows.data?.items ?? [])
    .filter((w) => w.status === 'scheduled' || w.status === 'active')
    .sort((a, b) => a.starts_at.localeCompare(b.starts_at));
  const nextWindow = upcoming[0];
  const lastWork = [...(records.data?.items ?? [])]
    .sort((a, b) => b.performed_at.localeCompare(a.performed_at))[0];
  const days = coverDays(d);
  const coverTone = d.warranty_state === 'expired' ? 'tone-critical'
    : d.warranty_state === 'expiring' ? 'tone-warn' : d.warranty_state === 'active' ? 'tone-ok' : '';
  const feedTone = chain.data
    ? (chain.data.live_paths > 1 ? 'tone-ok' : chain.data.live_paths === 1 ? 'tone-warn' : 'tone-critical')
    : '';

  const bodies: Record<Tab, ReactNode> = {
    overview: (
      <>
        <section className="dv-card">
          <h3>
            Where it stands
            <span className="lead">
              {life.data?.allowed.length
                ? `may move to ${life.data.allowed.map(humanise).join(', ')}`
                : life.data ? 'a terminal state: it cannot move again' : ''}
            </span>
            <button type="button" className="linklike more" onClick={() => goTab('lifecycle')}>
              Record a move →
            </button>
          </h3>
          <LifeTrack current={d.lifecycle ?? 'in_service'} history={life.data} />
        </section>

        <div className="dv-grid">
          <div className="dv-col">
            <section className="dv-card">
              <h3>
                Record
                {atLeast('operator') && (
                  <button type="button" className="linklike more" onClick={() => setEditingDevice(true)}>
                    Edit device →
                  </button>
                )}
              </h3>
              <div className="dv-facts">
                <Fact k="Asset tag" v={d.asset_tag} mono />
                <Fact k="Serial number" v={d.serial_number} mono />
                <Fact k="Type" v={humanise(d.device_type)} />
                <Fact k="Category" v={d.category ? humanise(d.category) : null} />
                <Fact k="Vendor" v={d.vendor} />
                <Fact k="Model" v={d.model} />
                <Fact k="Rated power" v={d.rated_power_w ? `${d.rated_power_w} W` : null} />
                <Fact k="Height" v={`${d.u_height}U`} />
                <Fact k="Admin state" v={humanise(d.admin_state)} />
                <Fact k="Management IP" v={d.mgmt_ip} mono />
                <Fact k="Primary IP" v={d.primary_ip} mono />
              </div>
              {d.notes && <p className="dv-note">{d.notes}</p>}
            </section>

            <section className="dv-card">
              <h3>Placement</h3>
              <div className="dv-facts">
                <Fact k="Site" v={loc.datacenter_code} />
                <Fact k="Room" v={loc.room_name} />
                <Fact k="Row" v={loc.row_name} />
                <Fact k="Rack" v={loc.rack_id && loc.rack_name
                  ? <Link to={`/assets/estate/racks/${loc.rack_id}`}>{loc.rack_name}</Link>
                  : loc.rack_name} />
                <Fact k="Position" v={loc.u_start
                  ? `U${loc.u_start}–U${loc.u_start + d.u_height - 1}` : null} />
                <Fact k="Facing" v={d.facing ? humanise(d.facing) : null} />
              </div>
            </section>

            <section className="dv-card">
              <h3>
                Tags
                <button type="button" className="linklike more" onClick={() => setEditingTags(true)}>
                  Edit tags →
                </button>
              </h3>
              {d.tags && d.tags.length > 0 ? (
                <span className="asset-tagrow">
                  {d.tags.map((t) => (
                    <span className="asset-chip" key={t.id}
                          style={t.colour ? { borderColor: t.colour } : undefined}>
                      {t.key}={t.value}
                    </span>
                  ))}
                </span>
              ) : <p className="dv-empty">No tags on this asset.</p>}
            </section>
          </div>

          <div className="dv-col">
            <section className="dv-card">
              <h3>
                Cover
                <button type="button" className="linklike more" onClick={() => goTab('support')}>
                  Support →
                </button>
              </h3>
              <p className={`asset-cover-line is-${d.warranty_state ?? 'unknown'}`}>{coverSentence(d)}</p>
              <p className="dv-note">
                {contracts.isLoading ? 'Reading contracts…'
                  : active.length === 0 ? 'No contract covers this asset.'
                  : `${active.length} active contract${active.length === 1 ? '' : 's'}: ${active.map((c) => c.reference).join(', ')}.`}
              </p>
            </section>

            <section className="dv-card">
              <h3>Ownership</h3>
              <div className="dv-facts">
                <Fact k="Owner" v={d.owner_group} />
                <Fact k="Cost centre" v={d.cost_centre} />
                <Fact k="Priority" v={d.priority ? humanise(d.priority) : 'Normal'} />
              </div>
            </section>

            <section className="dv-card">
              <h3>
                Work
                <button type="button" className="linklike more" onClick={() => goTab('maintenance')}>
                  Maintenance →
                </button>
              </h3>
              <ul className="dv-rows">
                <li className="dv-row">
                  <span className="k">Next window</span>
                  <span className="v">
                    {nextWindow
                      ? <><Link to={`/assets/maintenance/${nextWindow.id}`}>{nextWindow.title}</Link>
                          <span className="muted"> · {relativeTime(nextWindow.starts_at)}</span></>
                      : <span className="dv-none">none planned</span>}
                  </span>
                </li>
                <li className="dv-row">
                  <span className="k">Last work</span>
                  <span className="v">
                    {lastWork
                      ? <>{lastWork.summary}<span className="muted"> · {relativeTime(lastWork.performed_at)}</span></>
                      : <span className="dv-none">nothing recorded</span>}
                  </span>
                </li>
              </ul>
            </section>

            <section className="dv-card">
              <h3>
                Feed
                {chain.data && (
                  <span className={`dv-verdict ${chain.data.live_paths > 1 ? 'ok' : 'warn'}`}>
                    {humanise(chain.data.redundancy)}
                  </span>
                )}
                <button type="button" className="linklike more" onClick={() => goTab('connections')}>
                  Connections →
                </button>
              </h3>
              {chain.data ? (
                <>
                  <p className="dv-empty">{chain.data.reason}</p>
                  {chain.data.paths.length > 0 && (
                    <ul className="dv-rows" style={{ marginTop: 8 }}>
                      {chain.data.paths.map((p, i) => {
                        const first = p.hops[0];
                        const src = p.hops[p.hops.length - 1];
                        return (
                          <li key={p.side ?? i} className="dv-row">
                            <span className="k">{p.side ? `Side ${p.side}` : 'Feed'}</span>
                            <span className="v">
                              {first && <Link to={`/assets/inventory/${first.device_id}`}>{first.name}</Link>}
                              {src && src !== first && <span className="muted"> → {src.name}</span>}
                              {!p.reaches_source && <span className="warn"> · does not reach a source</span>}
                            </span>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </>
              ) : <p className="dv-empty">{chain.isLoading ? 'Tracing…' : 'No power chain recorded.'}</p>}
            </section>
          </div>
        </div>
      </>
    ),
    lifecycle: <section><LifecycleTab deviceId={d.id} /></section>,
    support: <section><SupportTab device={d} /></section>,
    maintenance: <section><MaintenanceTab deviceId={d.id} /></section>,
    connections: (
      <>
        <section>
          <h3>
            Power feeds
            {chain.data && (
              <span className="lead">{humanise(chain.data.redundancy)}: {chain.data.reason}</span>
            )}
          </h3>
          {chain.data && chain.data.paths.length > 0 ? (
            <div className="asset-cols">
              {chain.data.paths.map((path, i) => (
                <div className="asset-panel" key={path.side ?? i}>
                  <h3>
                    {path.side ? `Side ${path.side}` : 'Feed'}
                    {!path.reaches_source && (
                      // A path that stops short is not a short path: the trace
                      // ran out before a source, and "fed" would be wrong.
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
            <p className="dv-empty">{chain.isLoading ? 'Tracing…' : 'No power chain recorded for this asset.'}</p>
          )}
        </section>

        <section>
          <h3>
            Network ports
            {interfaces.data && interfaces.data.length > 0 && (
              <span className="count"> {interfaces.data.length}</span>
            )}
          </h3>
          {interfaces.isLoading && <p className="dv-empty">Loading…</p>}
          {interfaces.data && interfaces.data.length === 0 && (
            <p className="dv-empty">No ports recorded for this asset.</p>
          )}
          {interfaces.data && interfaces.data.length > 0 && (
            <div className="table-frame is-scrollable">
              <table>
                <thead>
                  <tr><th>Port</th><th>Role</th><th>Speed</th><th>MAC</th><th>Peer</th></tr>
                </thead>
                <tbody>
                  {interfaces.data.map((n) => (
                    <tr key={n.id}>
                      <td>{n.name}</td>
                      <td className="muted">{humanise(n.role)}</td>
                      <td className="muted">
                        {n.speed_bps ? `${Math.round(n.speed_bps / 1e9)} Gb/s` : '—'}
                      </td>
                      <td className="mono muted">{n.mac ?? '—'}</td>
                      <td>
                        {n.peer_device_id
                          ? <><Link to={`/assets/inventory/${n.peer_device_id}`}>{n.peer_device}</Link>
                              {n.peer_port && <span className="muted"> · {n.peer_port}</span>}</>
                          : <span className="muted">not patched</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </>
    ),
  };

  return (
    <div className="dv-page">
      <nav className="dv-crumb" aria-label="Breadcrumb">
        <Link to="/assets/inventory">Inventory</Link>
        {loc.datacenter_code && <><span aria-hidden>›</span><span>{loc.datacenter_code}</span></>}
        {loc.room_name && <><span aria-hidden>›</span><span>{loc.room_name}</span></>}
        {loc.rack_name && (
          <>
            <span aria-hidden>›</span>
            {loc.rack_id
              ? <Link to={`/assets/estate/racks/${loc.rack_id}`}>{loc.rack_name}</Link>
              : <span>{loc.rack_name}</span>}
          </>
        )}
      </nav>

      <header className="dv-head">
        <div className="dv-title">
          <h2>
            {d.name}
            <LifecycleChip state={d.lifecycle} />
          </h2>
          <p className="subtitle">
            {humanise(d.device_type)} · {makeModel(d)}
            {d.asset_tag ? ` · ${d.asset_tag}` : ' · no asset tag'}
            {d.serial_number ? ` · ${d.serial_number}` : ''}
          </p>
          <div className="dv-tools">
            {atLeast('operator') && (
              <button onClick={() => setEditingDevice(true)}>Edit device</button>
            )}
            <button onClick={() => setEditingTags(true)}>Edit tags</button>
            {/* Out to the operational view: telemetry, alarms, actions. */}
            <Link to={`/devices/${d.id}`}>Open in Devices →</Link>
          </div>
        </div>

        <div className="dv-band" aria-label="Asset standing">
          <div className={`dv-tile no-glyph ${coverTone}`}
               title={d.warranty_expires ? `until ${d.warranty_expires}` : 'no cover recorded'}>
            <b>{days == null ? '—' : days < 0 ? `${Math.abs(days)} d over` : `${days} d`}</b>
            <span>{days == null ? 'no cover' : days < 0 ? 'cover expired' : 'cover left'}</span>
          </div>
          <div className="dv-tile no-glyph">
            <b>{contracts.isLoading ? <span className="dv-skel">0</span> : active.length}</b>
            <span>{active.length === 1 ? 'active contract' : 'active contracts'}</span>
          </div>
          <div className="dv-tile no-glyph" title={nextWindow ? nextWindow.title : 'nothing planned'}>
            <b>{windows.isLoading ? <span className="dv-skel">00</span>
                : nextWindow ? relativeTime(nextWindow.starts_at) : '—'}</b>
            <span>next window</span>
          </div>
          <div className="dv-tile no-glyph" title={lastWork ? lastWork.summary : 'nothing recorded'}>
            <b>{records.isLoading ? <span className="dv-skel">00</span>
                : lastWork ? relativeTime(lastWork.performed_at) : '—'}</b>
            <span>last work</span>
          </div>
          <div className={`dv-tile no-glyph ${feedTone}`} title={chain.data?.reason ?? ''}>
            <b>{chain.isLoading ? <span className="dv-skel">00</span>
                : chain.data ? humanise(chain.data.redundancy) : '—'}</b>
            <span>power feed</span>
          </div>
        </div>
      </header>

      {editingDevice && (
        <DeviceEditor device={d} onClose={() => setEditingDevice(false)} />
      )}
      {editingTags && (
        <TagAssignForm objectType="device" objectId={d.id}
                       onClose={() => setEditingTags(false)} />
      )}

      <div className="dv-tabs" role="tablist" aria-label="Asset sections">
        {TABS.map((t) => (
          <button key={t.id} type="button" role="tab" id={`ar-tab-${t.id}`}
                  aria-selected={tab === t.id} aria-controls="ar-panel"
                  onClick={() => goTab(t.id)}>
            {t.label}
            {t.id === 'maintenance' && upcoming.length > 0 && (
              <span className="n">{upcoming.length}</span>
            )}
          </button>
        ))}
      </div>

      <div className="dv-tab" id="ar-panel" role="tabpanel" aria-labelledby={`ar-tab-${tab}`}>
        {bodies[tab]}
      </div>
    </div>
  );
}

/** The lifecycle as a track: every state in the order it is lived, the one
 *  the asset is in lit, the ones it has been through marked, and the ones
 *  it may go to next outlined. The matrix is the server's; this only draws
 *  what it says. */
function LifeTrack({ current, history }: { current: string; history?: LifecycleHistory }) {
  const visited = new Set<string>();
  for (const e of history?.events ?? []) {
    if (e.from_state) visited.add(e.from_state);
    visited.add(e.to_state);
  }
  const allowed = new Set(history?.allowed ?? []);
  return (
    <ol className="ar-track" aria-label="Lifecycle">
      {LIFE.map((s) => {
        const cls = s === current ? 'is-current'
          : allowed.has(s) ? 'is-next'
          : visited.has(s) ? 'is-visited' : '';
        return (
          <li key={s} className={`ar-step ${cls}`}>
            <span className="ar-dot" aria-hidden />
            <span className="ar-name">{humanise(s)}</span>
            {s === current && <span className="ar-hint">now</span>}
            {cls === 'is-next' && <span className="ar-hint">next</span>}
          </li>
        );
      })}
    </ol>
  );
}
