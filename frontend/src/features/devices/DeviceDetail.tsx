import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import {
  api,
  atLeast,
  type DeviceDetail as Detail,
  type DeviceState,
  type MetricValue,
  type NetworkInterface,
} from '../../api/client';
import { StatusChip } from '../../components/StatusChip';
import { PanelMetering, meteringWords } from '../../components/PanelMetering';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { ActionGlyph } from '../../components/ActionGlyph';
import { TileGlyph, type GlyphName } from '../floorplan/glyphs';
import { DeviceHistory } from './DeviceHistory';
import { chartGroupsFor } from './chartGroups';
import { bodySize } from './bodySize';
import { DeviceEditor } from './DeviceEditor';
import { ModelBudget } from './ModelBudget';
import { DeviceActions } from './DeviceActions';
import { DeviceThresholds } from './DeviceThresholds';
import { DeviceAlarms } from './DeviceAlarms';
import { DeviceEndpoints } from './DeviceEndpoints';
import { DeviceConnections, portSummary, psuVerdict } from './DeviceConnections';
import { METRICS, type MetricKey } from '../../lib/metrics.gen';
import {
  formatMetric, humanise, metricLabel, relativeTime, statusClass,
} from '../../lib/format';
import './devices.css';
import { Notice } from '../../components/Notice';

/** One device, the way an operator reads it.
 *
 *  The head answers "what is it, where is it, is it all right" without a
 *  scroll: name and placement, a readout band of the four numbers that matter
 *  for its class, and a severity rail when something is open. The tabs are
 *  the questions that follow, one each: what it is on record, what it reports,
 *  what is wrong, what it is plugged into, how it is polled, what can be done
 *  to it. The asset record (lifecycle, warranty, contracts) is its own page
 *  and is linked, not copied. */

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'telemetry', label: 'Telemetry' },
  { id: 'alarms', label: 'Alarms' },
  { id: 'connections', label: 'Connections' },
  { id: 'monitoring', label: 'Monitoring' },
  { id: 'operations', label: 'Operations' },
] as const;
type Tab = typeof TABS[number]['id'];
const isTab = (s: string | null): s is Tab => TABS.some((t) => t.id === s);

/** "Supermicro SYS-221H-TNR" once, not "Supermicro Supermicro SYS-221H-TNR":
 *  many model strings already carry the make. */
function makeModel(d: Detail): string {
  const v = d.vendor ?? 'unknown vendor';
  const m = d.model ?? '';
  if (m && d.vendor && m.toLowerCase().startsWith(d.vendor.toLowerCase())) return m;
  return `${v} ${m}`.trim();
}

export function DeviceDetail() {
  const { id = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const tab: Tab = isTab(params.get('tab')) ? (params.get('tab') as Tab) : 'overview';
  const goTab = (t: Tab) => {
    const next = new URLSearchParams(params);
    if (t === 'overview') next.delete('tab'); else next.set('tab', t);
    setParams(next, { replace: true });
  };
  const [editingDevice, setEditingDevice] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  // `?edit=1` opens the editor on arrival - the floor-plan panel's Edit
  // button lands here - and the flag is dropped from the address so a reload
  // or a shared link does not reopen it.
  useEffect(() => {
    if (params.get('edit') !== '1') return;
    setEditingDevice(true);
    const next = new URLSearchParams(params);
    next.delete('edit');
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const device = useQuery<Detail>({
    queryKey: ['device', id],
    queryFn: () => api.device(id),
    enabled: Boolean(id),
  });

  // Ports are inventory: they change when somebody re-cables a rack, not on a
  // poll interval, so this is fetched once rather than refreshed like state.
  const ports = useQuery<NetworkInterface[]>({
    queryKey: ['device-interfaces', id],
    queryFn: () => api.interfaces(id),
    enabled: Boolean(id),
    retry: false,
  });

  const state = useQuery<DeviceState>({
    queryKey: ['device-state', id],
    queryFn: () => api.deviceState(id),
    enabled: Boolean(id),
    // 30s: the slowest source behind this is a 120s BMC poll and the fastest a
    // 10s sensor, so this beats every source without asking for numbers that
    // cannot have changed.
    refetchInterval: 30_000,
    // A device with no telemetry yet is a normal state, not an error.
    retry: false,
  });

  // A device with no metering of its own (a remote power panel) is read
  // through the meter clamped on it. Empty for everything else.
  const metering = useQuery({
    queryKey: ['device-metering', id],
    queryFn: () => api.deviceMetering(id),
    enabled: Boolean(id),
    refetchInterval: 30_000,
    retry: false,
  });

  if (device.isLoading) return <p className="muted">Loading…</p>;
  if (device.error) return <Notice tone="error">Failed to load: {String(device.error)}</Notice>;
  if (!device.data) return null;

  const d = device.data;
  const metrics = state.data?.metrics ?? {};
  const meters = metering.data?.meters ?? [];
  const passive = meters.length > 0 && d.status === 'UNKNOWN' && Object.keys(metrics).length === 0;
  const words = passive ? meteringWords(meters) : null;
  // Only a device that does not meter itself borrows: switchgear and MCCs
  // feed sub-meters too, and their own readings are what their pages show.
  const meteringCard = passive && (
    <section className="dv-card">
      <h3>Metering<span className="lead"> read by the meter on this panel, not by the panel</span></h3>
      <PanelMetering meters={meters} variant="summary" />
    </section>
  );
  const openAlarms = state.data?.active_alarms ?? 0;
  const sev = (state.data?.max_severity ?? d.max_severity ?? '').toLowerCase();
  const loc = d.location;
  const groups = chartGroupsFor(d.device_type);
  const chartMetrics = groups
    ? [...new Set(groups.flatMap((g) => g.metrics))]
    : Object.keys(metrics);

  const bodies: Record<Tab, ReactNode> = {
    overview: <Overview d={d} ports={ports.data} metrics={metrics} state={state.data} lead={meteringCard}
                        onAlarms={() => goTab('alarms')} onMonitoring={() => goTab('monitoring')} />,
    telemetry: (
      <>
        {passive && (
          <section>
            <h3>Metering</h3>
            <PanelMetering meters={meters} variant="page" />
          </section>
        )}
        {!passive && <Readings d={d} metrics={metrics} loading={state.isLoading} />}
        {passive
          ? <p className="dv-note">
              History is kept on the meter, not the panel: see{' '}
              <Link to={`/devices/${meters[0].meter_id}?tab=telemetry`}>{meters[0].meter_name}</Link> for the charts.
            </p>
          : <DeviceHistory deviceId={id} metrics={chartMetrics} groups={groups} />}
      </>
    ),
    // Open conditions first, then the thresholds an operator came to change,
    // then the whole history folded away: it can run to a hundred rows and
    // must never push the other two off the screen.
    alarms: (
      <>
        <section>
          <h3>Open conditions</h3>
          <DeviceAlarms deviceId={id} deviceName={d.name} view="open" />
        </section>
        <DeviceThresholds deviceId={id} />
        <section>
          <details className="dv-fold" open={historyOpen}
                   onToggle={(e) => setHistoryOpen((e.target as HTMLDetailsElement).open)}>
            <summary>History<span className="lead"> everything recorded on this device, cleared included</span></summary>
            {historyOpen && <DeviceAlarms deviceId={id} deviceName={d.name} view="all" />}
          </details>
        </section>
      </>
    ),
    connections: <DeviceConnections device={d} ports={ports} />,
    monitoring: <DeviceEndpoints device={d} />,
    operations: (
      <>
        <DeviceActions deviceId={id} />
        <section>
          <h3>Record</h3>
          <p className="dv-empty">
            Name, make, model, addresses and ownership are edited here and kept
            across a re-import. Lifecycle, warranty and contracts live on the{' '}
            <Link to={`/assets/inventory/${d.id}`}>asset record</Link>.
          </p>
          {atLeast('operator') && (
            <p style={{ margin: '12px 0 0' }}>
              <button onClick={() => setEditingDevice(true)}>Edit device</button>
            </p>
          )}
          {(d.locked_fields?.length ?? 0) > 0 && (
            <>
              <p className="dv-note">Set by hand and kept across re-imports:</p>
              <div className="dv-locks">
                {d.locked_fields!.map((f) => <span key={f} className="chip">{humanise(f)}</span>)}
              </div>
            </>
          )}
          <p className="dv-note">
            Plant setpoints are not set from here: the building management
            system holds the interlocks.
          </p>
        </section>
      </>
    ),
  };

  return (
    <div className="dv-page">
      <nav className="dv-crumb" aria-label="Breadcrumb">
        <Link to="/devices">Devices</Link>
        {loc.datacenter_code && <><span aria-hidden>›</span><span>{loc.datacenter_code}</span></>}
        {loc.room_name && <><span aria-hidden>›</span><span>{loc.room_name}</span></>}
        {loc.rack_name && (
          <>
            <span aria-hidden>›</span>
            {loc.rack_id
              ? <Link to={`/racks/${loc.rack_id}`}>{loc.rack_name}</Link>
              : <span>{loc.rack_name}</span>}
          </>
        )}
      </nav>

      <header className={`dv-head${sev && openAlarms > 0 ? ` sev-${sev}` : ''}`}>
        <div className="dv-title">
          <h2>
            {d.name}
            {words
              ? <span className={`chip${words.lost ? ' warn' : ''}`} title={words.text}>
                  {words.lost ? 'Metering lost' : 'Not monitored'}
                </span>
              : <StatusChip status={d.status} />}
            {d.priority && d.priority !== 'normal' && (
              <span className={`chip prio-${d.priority}`}>{d.priority} priority</span>
            )}
            {d.admin_state && d.admin_state !== 'enabled' && (
              <span className="chip">{humanise(d.admin_state)}</span>
            )}
          </h2>
          <p className="subtitle">
            {humanise(d.device_type)} · {makeModel(d)}
            {loc.rack_name ? ` · ${loc.rack_name}` : ''}
            {loc.u_start ? ` U${loc.u_start}` : ''}
            {bodySize(d).short ? `, ${bodySize(d).short}` : ''}
            {passive && meters[0] && (
              <> · metered by <Link to={`/devices/${meters[0].meter_id}`}>{meters[0].meter_name}</Link></>
            )}
          </p>
          <div className="dv-tools">
            {atLeast('operator') && (
              <button type="button" className="dv-act" onClick={() => setEditingDevice(true)}>
                <ActionGlyph kind="edit" />Edit device
              </button>
            )}
            {/* The device in the room, selected, its panel open. A place
                link rather than a back link: it is right from any arrival,
                and Back stays the way to retrace steps. */}
            {loc.room_id && loc.rack_id && (
              <Link className="dv-act" to={`/floorplan?room=${loc.room_id}&rack=${loc.rack_id}&device=${d.id}`}>
                <ActionGlyph kind="cube" />View in 3D
              </Link>
            )}
          </div>
        </div>
        <Band d={d} state={state.data} loading={state.isLoading} />
      </header>
      {editingDevice && (
        <DeviceEditor device={d} onClose={() => setEditingDevice(false)} />
      )}

      <div className="dv-tabs" role="tablist" aria-label="Device sections">
        {TABS.map((t) => (
          <button key={t.id} type="button" role="tab" id={`dv-tab-${t.id}`}
                  aria-selected={tab === t.id} aria-controls="dv-panel"
                  onClick={() => goTab(t.id)}>
            {t.label}
            {t.id === 'alarms' && openAlarms > 0 && (
              <span className={`n sev-${sev}`}>{openAlarms}</span>
            )}
          </button>
        ))}
      </div>

      <div className="dv-tab" id="dv-panel" role="tabpanel" aria-labelledby={`dv-tab-${tab}`}>
        {bodies[tab]}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ band

/** The readings an operator looks at first, by class. The curated chart
 *  groups say which metrics matter for a device type; the generic list
 *  catches the types nobody has curated. */
const PREFERRED = [
  'power_draw', 'load_pct', 'current', 'apparent_power', 'inlet_temperature',
  'ambient_temperature', 'cpu_temperature', 'cpu_utilization', 'memory_utilization',
  'relative_humidity', 'water_supply_temp', 'water_return_temp', 'water_flow',
  'supply_air_temp', 'return_air_temp', 'battery_runtime', 'fuel_level_pct',
  'component_temperature',
];
const NOT_A_READING = new Set(['sys_uptime', 'reachable', 'poll_latency', 'energy_consumed']);

const GLYPH: Record<string, GlyphName> = {
  power_draw: 'power', psu_output_power: 'power', apparent_power: 'power', reactive_power: 'power',
  inlet_temperature: 'air-in', supply_air_temp: 'air-in', ambient_temperature: 'air-in',
  exhaust_temperature: 'air-out', return_air_temp: 'air-out',
  cpu_temperature: 'heat', component_temperature: 'heat', motor_temp: 'heat',
  oil_temperature: 'heat', winding_temperature: 'heat', battery_temperature: 'heat',
  thermal_load: 'heat', cooling_capacity: 'heat',
  cpu_utilization: 'chip', memory_utilization: 'memory', disk_utilization: 'memory',
  load_pct: 'load', compressor_load_pct: 'load', cooling_output_pct: 'load',
  current: 'current', relative_humidity: 'humidity', dew_point: 'dew',
  water_supply_temp: 'water-in', water_return_temp: 'water-out',
  water_flow: 'flow', makeup_water_flow: 'flow', airflow: 'flow',
  water_setpoint_temp: 'setpoint', air_setpoint_temp: 'setpoint', approach_temp: 'spread',
};

const SHORT: Record<string, string> = {
  power_draw: 'power', inlet_temperature: 'inlet', exhaust_temperature: 'exhaust',
  cpu_temperature: 'cpu temp', cpu_utilization: 'cpu', memory_utilization: 'memory',
  disk_utilization: 'disk', ambient_temperature: 'ambient', relative_humidity: 'humidity',
  dew_point: 'dew point', water_supply_temp: 'supply', water_return_temp: 'return',
  water_flow: 'flow', load_pct: 'load', current: 'current', apparent_power: 'load',
  voltage_ln: 'voltage', supply_air_temp: 'supply air', return_air_temp: 'return air',
  fan_speed_pct: 'fan', battery_runtime: 'runtime', fuel_level_pct: 'fuel',
  component_temperature: 'temp', psu_output_power: 'psu out',
};

function pickReadout(type: string, m: Record<string, MetricValue>): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  const candidates = [
    ...(chartGroupsFor(type) ?? []).flatMap((g) => g.metrics),
    ...PREFERRED,
    ...Object.keys(m).sort(),
  ];
  for (const k of candidates) {
    if (seen.has(k) || out.length >= 4) continue;
    seen.add(k);
    if (NOT_A_READING.has(k) || k.startsWith('if_')) continue;
    if (typeof m[k]?.v !== 'number') continue;
    out.push(k);
  }
  return out;
}

function withUnit(k: string, m: MetricValue): string {
  const unit = METRICS[k as MetricKey]?.unit;
  const val = formatMetric(k, m.v);
  const bare = typeof m.v === 'number' && unit && !/[a-zA-Z°%]/.test(val);
  return bare ? `${val} ${unit}` : val;
}

function Band({ d, state, loading }: { d: Detail; state?: DeviceState; loading: boolean }) {
  const m = state?.metrics ?? {};
  const keys = useMemo(() => pickReadout(d.device_type, m), [d.device_type, m]);
  const open = state?.active_alarms ?? 0;
  const sev = (state?.max_severity ?? '').toLowerCase();
  const alarmTone = open === 0 ? 'tone-ok' : sev === 'critical' ? 'tone-critical'
    : sev === 'major' ? 'tone-major' : 'tone-warn';

  if (loading) {
    return (
      <div className="dv-band" aria-busy="true" aria-label="Reading the device">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="dv-tile">
            <span className="dv-glyph" aria-hidden />
            <b className="dv-skel">00.0</b><span className="dv-skel">reading</span>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="dv-band" aria-label="Live readings">
      {keys.map((k) => {
        const mv = m[k];
        const g = GLYPH[k];
        const flagged = mv.q !== 'good';
        return (
          <div key={k} className={`dv-tile${flagged ? ' is-flagged' : ''}`}
               title={`${metricLabel(k)}${mv.t ? ` · ${relativeTime(mv.t)}` : ''}${flagged ? ` · quality: ${mv.q}` : ''}`}>
            <span className="dv-glyph" aria-hidden>{g && <TileGlyph name={g} />}</span>
            <b>{withUnit(k, mv)}</b>
            <span>{SHORT[k] ?? metricLabel(k).toLowerCase()}</span>
          </div>
        );
      })}
      <div className={`dv-tile ${alarmTone}`} title={open ? `worst: ${sev}` : 'nothing open'}>
        <span className="dv-glyph" aria-hidden><CategoryGlyph kind="alarms" size={18} /></span>
        <b>{open}</b>
        <span>{open === 1 ? 'open alarm' : 'open alarms'}</span>
      </div>
      <div className="dv-tile no-glyph" title={state?.last_seen ? new Date(state.last_seen).toLocaleString() : 'never polled'}>
        <b>{state?.last_seen ? relativeTime(state.last_seen) : '—'}</b>
        <span>last seen</span>
      </div>
    </div>
  );
}

// -------------------------------------------------------------- overview

function Fact({ k, v, mono }: { k: string; v: ReactNode; mono?: boolean }) {
  const empty = v == null || v === '' || v === '—';
  return (
    <div className="dv-fact">
      <div className="k">{k}</div>
      <div className={`v${mono ? ' mono' : ''}`}>{empty ? <span className="dv-none">—</span> : v}</div>
    </div>
  );
}

function Overview({ d, ports, metrics, state, lead, onAlarms, onMonitoring }: {
  d: Detail;
  /** A card that leads the first column: a passive panel's metering. */
  lead?: ReactNode;
  ports?: NetworkInterface[];
  metrics: Record<string, MetricValue>;
  state?: DeviceState;
  onAlarms: () => void;
  onMonitoring: () => void;
}) {
  const loc = d.location;
  const draw = typeof metrics.power_draw?.v === 'number' ? metrics.power_draw.v : null;
  const rated = d.rated_power_w ?? null;
  const pct = draw != null && rated ? Math.round((draw / rated) * 100) : null;
  const verdict = psuVerdict(d);
  const fitted = ports?.length ?? 0;
  const cabled = ports?.filter((p) => p.peer_device).length ?? 0;
  const summary = portSummary(ports);

  return (
    <div className="dv-grid">
      <div className="dv-col">
        {lead}
        <section className="dv-card">
          <h3>Record <Link className="more" to={`/assets/inventory/${d.id}`}>Asset record →</Link></h3>
          <div className="dv-facts">
            <Fact k="Type" v={humanise(d.device_type)} />
            <Fact k="Vendor" v={d.vendor} />
            <Fact k="Model" v={d.model} />
            <Fact k="Serial" v={d.serial_number} mono />
            <Fact k="Asset tag" v={d.asset_tag} mono />
            <Fact k="Owner" v={d.owner_group
              ? <>{d.owner_group}{d.cost_centre && <span className="muted"> · {d.cost_centre}</span>}</>
              : null} />
            <Fact k="Management IP" v={d.mgmt_ip} mono />
            <Fact k="Production IP" v={d.primary_ip} mono />
            <Fact k="Lifecycle" v={humanise(d.lifecycle)} />
            <Fact k="Priority" v={d.priority ? humanise(d.priority) : 'Normal'} />
          </div>
          {d.notes && <p className="dv-note">{d.notes}</p>}
        </section>

        <section className="dv-card">
          <h3>Placement</h3>
          <div className="dv-facts">
            <Fact k="Datacenter" v={loc.datacenter_code} />
            <Fact k="Room" v={loc.room_name} />
            <Fact k="Row" v={loc.row_name} />
            <Fact k="Rack" v={loc.rack_id && loc.rack_name
              ? <Link to={`/racks/${loc.rack_id}`}>{loc.rack_name}</Link> : loc.rack_name} />
            <Fact k="Position" v={loc.u_start
              ? `U${loc.u_start}, ${d.u_height}U${d.facing ? `, facing ${d.facing}` : ''}` : null} />
          </div>
        </section>

        <section className="dv-card">
          <h3>Nameplate</h3>
          <div className="dv-facts">
            <Fact k="Rated power" v={rated != null ? `${rated} W` : null} />
            <ModelBudget d={d} />
            <Fact k="Power supplies" v={d.psus.length
              ? `${d.psus.length} × ${d.psus[0].rated_watts ?? '?'} W (${[...new Set(d.psus.map((p) => p.connector ?? '?'))].join(', ')})`
              : null} />
            <Fact k="Network ports" v={summary
              ? <>{summary}<span className="muted"> · {cabled} of {fitted} patched</span></> : null} />
            <Fact k={bodySize(d).label} v={bodySize(d).long} />
          </div>
          {rated != null && (
            <div className="dv-meter" aria-label="Draw against the nameplate rating">
              <div className="dv-meter-track">
                <div className={`dv-meter-fill${pct != null && pct >= 95 ? ' critical' : pct != null && pct >= 80 ? ' warn' : ''}`}
                     style={{ width: `${Math.min(100, pct ?? 0)}%` }} />
              </div>
              <div className="dv-meter-label">
                <span>{draw != null ? `drawing ${Math.round(draw)} W` : 'no draw reported'}</span>
                <span>{pct != null ? `${pct}% of ${rated} W` : `${rated} W rated`}</span>
              </div>
            </div>
          )}
        </section>
      </div>

      <div className="dv-col">
        <section className="dv-card">
          <h3>
            Open conditions
            <button type="button" className="linklike more" onClick={onAlarms}>All alarms →</button>
          </h3>
          <DeviceAlarms deviceId={d.id} deviceName={d.name} compact limit={4} onMore={onAlarms} />
        </section>

        {d.psus.length > 0 && (
        <section className="dv-card">
          <h3>
            Power feeds
            {verdict && <span className={`dv-verdict ${verdict.warn ? 'warn' : 'ok'}`} style={{ marginLeft: 'auto' }}>{verdict.text}</span>}
          </h3>
          {d.psus.length === 0 ? (
            <p className="dv-empty">No power supplies recorded.</p>
          ) : (
            <ul className="dv-rows">
              {d.psus.map((p) => (
                <li key={p.number} className="dv-row">
                  <span className="k">PSU{p.number}{p.rated_watts != null ? ` · ${p.rated_watts} W` : ''}</span>
                  <span className="v">
                    {p.feed_device_id ? (
                      <>
                        <Link to={`/devices/${p.feed_device_id}`}>{p.feed_device}</Link>
                        {p.feed_outlet != null && <span className="muted"> · outlet {p.feed_outlet}</span>}
                      </>
                    ) : <span className="warn">not corded</span>}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
        )}

        <section className="dv-card">
          <h3>
            Monitoring
            <button type="button" className="linklike more" onClick={onMonitoring}>Details →</button>
          </h3>
          {d.endpoints.length === 0 ? (
            <p className="dv-empty">No endpoints configured.</p>
          ) : (
            <ul className="dv-rows">
              {d.endpoints.filter((e) => e.enabled).map((e) => (
                <li key={e.id} className="dv-row">
                  <span className="k">
                    {humanise(e.protocol)} <span className="muted">· {humanise(e.role)}</span>
                  </span>
                  <span className="v">
                    <span className={`chip ${statusClass(e.status)}`}>
                      <span className="dot" aria-hidden /> {e.status}
                    </span>
                    <span className="muted small"> {relativeTime(e.last_success)}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
          {state?.last_seen && (
            <p className="dv-note">Last reading {relativeTime(state.last_seen)}.</p>
          )}
        </section>
      </div>
    </div>
  );
}

// -------------------------------------------------------------- readings

/** Every current reading, grouped the way the charts are: the curated panels
 *  for the type first, then the catalogue's groups for the rest. */
function Readings({ d, metrics, loading }: {
  d: Detail; metrics: Record<string, MetricValue>; loading: boolean;
}) {
  const groups = useMemo(() => {
    const keys = Object.keys(metrics).filter((k) => !k.startsWith('if_'));
    const used = new Set<string>();
    const out: { title: string; keys: string[] }[] = [];
    for (const g of chartGroupsFor(d.device_type) ?? []) {
      const have = g.metrics.filter((k) => keys.includes(k));
      if (have.length) { out.push({ title: g.title, keys: have }); have.forEach((k) => used.add(k)); }
    }
    const rest = new Map<string, string[]>();
    for (const k of keys.sort()) {
      if (used.has(k)) continue;
      const grp = METRICS[k as MetricKey]?.group ?? 'other';
      rest.set(grp, [...(rest.get(grp) ?? []), k]);
    }
    for (const [grp, ks] of rest) out.push({ title: humanise(grp), keys: ks });
    return out;
  }, [metrics, d.device_type]);
  const n = groups.reduce((a, g) => a + g.keys.length, 0);

  return (
    <section>
      <h3>Current readings{n > 0 && <span className="count"> {n}</span>}</h3>
      {loading && <p className="dv-empty">Reading…</p>}
      {!loading && n === 0 && (
        <p className="dv-empty">
          No telemetry yet. The first poll lands within one interval of the
          collector picking this endpoint up.
        </p>
      )}
      {n > 0 && (
        <div className="dv-readings">
          {groups.map((g) => (
            <div key={g.title} className="dv-rgroup">
              <h4>{g.title}</h4>
              {g.keys.map((k) => {
                const m = metrics[k];
                const good = m.q === 'good';
                return (
                  <div key={k} className={`dv-reading${good ? '' : ' is-flagged'}`}
                       title={good ? undefined : `quality: ${m.q}`}>
                    <span className="k">{metricLabel(k)}</span>
                    <span className="v">{withUnit(k, m)}</span>
                    <span className="t">{m.t ? relativeTime(m.t) : ''}{good ? '' : ` · ${humanise(m.q)}`}</span>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
