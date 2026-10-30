import { useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  api,
  type RackDetail as Detail,
  type RackDeviceRow,
  type RackElevation,
} from '../../api/client';
import { CategoryGlyph } from '../../components/CategoryGlyph';
import { Notice } from '../../components/Notice';
import { DeviceAlarms } from '../devices/DeviceAlarms';
import { humanise, relativeTime, statusClass } from '../../lib/format';
import { Elevation, OVERLAYS, type Overlay } from './Elevation';
import { Feeds, Failover, RackPowerTab } from './RackPower';
import { Fact, RackThermalTab } from './RackThermal';
import '../devices/devices.css';
import '../devices/device-list.css';
import './rack-detail.css';

/** One rack, the way an operator reads it.
 *
 *  The head answers "where is it, how full, how loaded, how warm, is anything
 *  open" without a scroll. The tabs are the questions that follow, one each:
 *  what stands in it and where, what feeds it and what it draws, the air it
 *  breathes, the devices themselves, what is wrong. The elevation keeps its
 *  tab: it is the view a change ticket or a remote-hands call is written
 *  against, and the 3D room is one link away for the spatial question. */

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'elevation', label: 'Elevation' },
  { id: 'power', label: 'Power' },
  { id: 'thermal', label: 'Thermal' },
  { id: 'devices', label: 'Devices' },
  { id: 'alarms', label: 'Alarms' },
] as const;
type Tab = typeof TABS[number]['id'];
const isTab = (s: string | null): s is Tab => TABS.some((t) => t.id === s);

/** Where the reader came from, for the back link. The estate pages and the
 *  floor plan say so in ?from=; anything else is treated as the rack list. */
const FROM: Record<string, { label: string; path: string }> = {
  thermal: { label: 'Thermal', path: '/thermal' },
  power: { label: 'Power', path: '/power' },
  floorplan: { label: 'Floor plan', path: '/floorplan' },
  racks: { label: 'All racks', path: '/racks' },
};

const CAT_WORD: Record<string, string> = {
  it: 'IT', network: 'network', power: 'power', cooling: 'cooling',
  environment: 'sensors', facility: 'facility', other: 'other',
};

function kw(v: number | null | undefined, digits = 1): string {
  return v == null ? '—' : `${v.toFixed(digits)} kW`;
}

export function RackDetail() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  // ?overlay=thermal is what the floor plan and older links send: it lands
  // on the elevation with that overlay on.
  const wantedOverlay = params.get('overlay');
  const tab: Tab = isTab(params.get('tab')) ? (params.get('tab') as Tab)
    : wantedOverlay && !params.get('tab') ? 'elevation' : 'overview';
  const goTab = (t: Tab) => {
    const next = new URLSearchParams(params);
    if (t === 'overview') next.delete('tab'); else next.set('tab', t);
    setParams(next, { replace: true });
  };
  const from = FROM[params.get('from') ?? ''] ?? FROM.racks;
  // Back is a real history step when there is one, so the reader lands where
  // they were - the drilled-in table, the room - not on a fresh copy of it.
  const goBack = () => ((window.history.state?.idx ?? 0) > 0 ? navigate(-1) : navigate(from.path));
  const [overlay, setOverlay] = useState<Overlay>(
    OVERLAYS.some((o) => o.key === wantedOverlay) ? (wantedOverlay as Overlay) : 'status');

  const detail = useQuery<Detail>({
    queryKey: ['rack-detail', id],
    queryFn: () => api.rackDetail(id),
    enabled: Boolean(id),
    refetchInterval: 30_000,
    retry: false,
  });
  const elevation = useQuery<RackElevation>({
    queryKey: ['rack-elevation', id],
    queryFn: () => api.rackElevation(id),
    enabled: Boolean(id) && tab === 'elevation',
    refetchInterval: 15_000,
  });

  if (detail.isLoading) return <p className="muted">Loading…</p>;
  if (detail.error) return <Notice tone="error">Failed to load: {String(detail.error)}</Notice>;
  if (!detail.data) return null;

  const d = detail.data;
  const { rack, space, alarms, power, thermal } = d;
  const sev = alarms.worst.toLowerCase();
  const loadTone = power?.band === 'critical' ? 'tone-critical' : power?.band === 'warn' ? 'tone-warn' : '';
  const hotTone = thermal.hot === 'out' ? 'tone-critical' : thermal.hot ? 'tone-warn' : '';
  const alarmTone = alarms.open === 0 ? '' : sev === 'critical' ? 'tone-critical'
    : sev === 'major' ? 'tone-major' : 'tone-warn';
  const where = [rack.datacenter_code, rack.room_name,
    rack.row_name ? `Row ${rack.row_name}` : null,
    rack.position != null ? `position ${rack.position}` : null].filter(Boolean).join(' · ');

  const bodies: Record<Tab, ReactNode> = {
    overview: <Overview d={d} onAlarms={() => goTab('alarms')} onThermal={() => goTab('thermal')}
                        onPower={() => goTab('power')} />,
    elevation: (
      <section>
        <h3>Elevation
          <span className="lead">front, top of the rack first</span>
          {rack.room_id && (
            <Link className="more" to={`/floorplan?room=${rack.room_id}&rack=${rack.id}`}>In the room, 3D →</Link>
          )}
        </h3>
        {elevation.data
          ? <Elevation data={elevation.data} overlay={overlay} onOverlay={setOverlay} />
          : elevation.isError ? <Notice tone="error">The elevation did not load.</Notice>
          : <p className="muted">Loading…</p>}
      </section>
    ),
    power: power
      ? <RackPowerTab p={power} />
      : <section><p className="dv-empty">The power inventory does not know this rack.</p></section>,
    thermal: <RackThermalTab t={thermal} rackId={id} />,
    devices: <Devices rows={d.devices} />,
    alarms: (
      <section>
        <h3>Conditions <span className="lead">on everything standing in this rack</span></h3>
        <DeviceAlarms deviceId="" rackId={id} deviceName={rack.name} view="all" />
      </section>
    ),
  };

  return (
    <div className="dv-page">
      <nav className="dv-crumb" aria-label="Breadcrumb">
        <button type="button" className="link-button" onClick={goBack}>← {from.label}</button>
        <span aria-hidden>·</span>
        {rack.room_id
          ? <Link to={`/floorplan?room=${rack.room_id}`}>{rack.datacenter_code} › {rack.room_name}</Link>
          : <span>{where}</span>}
      </nav>

      <header className={`dv-head${alarms.open > 0 && sev !== 'clear' ? ` sev-${sev}` : ''}`}>
        <div className="dv-title">
          <h2>
            {rack.name}
            {alarms.open > 0 && <span className={`chip ${statusClass(alarms.worst)}`}>{alarms.open} open</span>}
            {alarms.offline > 0 && <span className="chip critical">{alarms.offline} offline</span>}
          </h2>
          <p className="subtitle muted">
            {where} · {space.u_total} U · {rack.device_count} device{rack.device_count === 1 ? '' : 's'}
            {space.zero_u ? `, ${space.zero_u} in the side channels` : ''}
          </p>
          <div className="dv-tools">
            {rack.room_id && (
              <Link className="dv-act" to={`/floorplan?room=${rack.room_id}&rack=${rack.id}`}>Open in the room</Link>
            )}
            <Link className="dv-act" to={`/devices?q=${encodeURIComponent(rack.name)}`}>Its devices in the list</Link>
          </div>
        </div>

        <div className="dv-band" aria-label="Rack readout">
          <div className={`dv-tile no-glyph ${loadTone}`}
               title={power?.rated_kw ? `${kw(power.it_ac_kw, 2)} of ${kw(power.rated_kw)} rated` : 'no rating recorded'}>
            <b>{kw(power?.it_ac_kw)}</b>
            <span>{power?.load_pct != null ? `${power.load_pct.toFixed(0)} % of rating` : 'IT draw'}</span>
          </div>
          <div className={`dv-tile no-glyph${(power?.headroom_kw ?? 0) < 0 ? ' tone-critical' : ''}`}
               title={power?.p95_kw != null ? `rating minus the ${power.p95_days}-day p95 of ${kw(power.p95_kw, 2)}` : 'needs a rating and a week of draw'}>
            <b>{kw(power?.headroom_kw)}</b>
            <span>headroom</span>
          </div>
          <div className="dv-tile no-glyph"
               title={space.largest_free ? `largest free run ${space.largest_free.u_height} U at U${space.largest_free.u_start}` : 'no free unit'}>
            <b>{space.u_free} U</b>
            <span>{space.largest_free ? `free · run of ${space.largest_free.u_height}` : 'free'}</span>
          </div>
          <div className={`dv-tile no-glyph ${hotTone}`}
               title={thermal.sensors ? `${thermal.in_band} of ${thermal.sensors} intakes inside ASHRAE ${thermal.ashrae.class} recommended` : 'nothing reports an intake'}>
            <b>{thermal.intake_max_c != null ? `${thermal.intake_max_c.toFixed(1)} °C` : '—'}</b>
            <span>{thermal.hot === 'out' ? 'intake, out of envelope' : thermal.hot ? 'intake, above recommended' : 'hottest intake'}</span>
          </div>
          <div className="dv-tile no-glyph" title="mean exhaust minus mean intake across the rack">
            <b>{thermal.delta_t_k != null ? `${thermal.delta_t_k.toFixed(1)} K` : '—'}</b>
            <span>rise</span>
          </div>
          <div className={`dv-tile ${alarmTone}`} title={alarms.open ? `worst: ${alarms.worst.toLowerCase()}` : 'nothing open'}>
            <span className="dv-glyph" aria-hidden><CategoryGlyph kind="alarms" size={18} /></span>
            <b>{alarms.open}</b>
            <span>{alarms.open === 1 ? 'open alarm' : 'open alarms'}</span>
          </div>
        </div>
      </header>

      <div className="dv-tabs" role="tablist" aria-label="Rack sections">
        {TABS.map((t) => (
          <button key={t.id} type="button" role="tab" id={`dv-tab-${t.id}`}
                  aria-selected={tab === t.id} aria-controls="dv-panel"
                  onClick={() => goTab(t.id)}>
            {t.label}
            {t.id === 'alarms' && alarms.open > 0 && <span className={`n sev-${sev}`}>{alarms.open}</span>}
            {t.id === 'devices' && <span className="n">{d.devices.length}</span>}
          </button>
        ))}
      </div>
      <div className="dv-tab" id="dv-panel" role="tabpanel" aria-labelledby={`dv-tab-${tab}`}>
        {bodies[tab]}
      </div>
    </div>
  );
}

// -------------------------------------------------------------- overview

function Overview({ d, onAlarms, onThermal, onPower }: {
  d: Detail; onAlarms: () => void; onThermal: () => void; onPower: () => void;
}) {
  const { rack, space, alarms, power, thermal } = d;
  const cats = Object.entries(space.by_category).sort((a, b) => b[1] - a[1]);
  return (
    <div className="dv-grid">
      <div className="dv-col">
        <section className="dv-card">
          <h3>Space <span className="lead">{space.u_used} of {space.u_total} U in use</span></h3>
          <UMap d={d} />
          <div className="dv-facts" style={{ marginTop: 12 }}>
            <Fact k="Free" v={`${space.u_free} U`} />
            <Fact k="Largest free run" v={space.largest_free ? `${space.largest_free.u_height} U, from U${space.largest_free.u_start}` : 'none'} />
            <Fact k="On the rails" v={`${space.racked} device${space.racked === 1 ? '' : 's'}`} />
            <Fact k="In the side channels" v={String(space.zero_u)} />
            {cats.map(([c, n]) => <Fact key={c} k={CAT_WORD[c] ?? humanise(c)} v={String(n)} />)}
          </div>
        </section>
        <section className="dv-card">
          <h3>Feeds
            {power?.feeds && <span className="lead">{power.feeds.dual_cord} of {power.feeds.loads} loads on both sides</span>}
            <button type="button" className="link-button more" onClick={onPower}>Power →</button>
          </h3>
          {power ? <Feeds p={power} compact /> : <p className="dv-empty">The power inventory does not know this rack.</p>}
          {power && (
            <div style={{ marginTop: 14 }}>
              <h4 className="rk-sub">If a side is lost</h4>
              <Failover p={power} />
            </div>
          )}
        </section>
      </div>
      <div className="dv-col">
        <section className="dv-card">
          <h3>Air
            <span className="lead">{thermal.in_band} of {thermal.sensors} intakes in band</span>
            <button type="button" className="link-button more" onClick={onThermal}>By height →</button>
          </h3>
          <div className="dv-facts">
            <Fact k="Intake, mean" v={thermal.intake_mean_c != null ? `${thermal.intake_mean_c.toFixed(1)} °C` : '—'} />
            <Fact k="Intake, hottest" v={thermal.intake_max_c != null ? `${thermal.intake_max_c.toFixed(1)} °C` : '—'}
                  tone={thermal.hot === 'out' ? 'critical' : thermal.hot ? 'warn' : undefined} />
            <Fact k="Exhaust, mean" v={thermal.exhaust_mean_c != null ? `${thermal.exhaust_mean_c.toFixed(1)} °C` : '—'} />
            <Fact k="Rise" v={thermal.delta_t_k != null ? `${thermal.delta_t_k.toFixed(1)} K` : '—'} />
            <Fact k="Band" v={`ASHRAE ${thermal.ashrae.class}, ${thermal.ashrae.rec_low_c}–${thermal.ashrae.rec_high_c} °C`} />
            <Fact k="Humidity" v={thermal.rh_mean_pct != null ? `${thermal.rh_mean_pct.toFixed(0)} %` : '—'} />
          </div>
        </section>
        <section className="dv-card">
          <h3>Open conditions
            <span className="lead">{alarms.open ? `${alarms.open} on ${alarms.devices} device${alarms.devices === 1 ? '' : 's'}` : 'nothing open'}</span>
          </h3>
          <DeviceAlarms deviceId="" rackId={rack.id} deviceName={rack.name} compact limit={5} onMore={onAlarms} />
        </section>
      </div>
    </div>
  );
}

/** Forty-two cells, U1 at the left: each filled cell wears its device's
 *  category colour, a free run reads as a gap, an alarmed device is ringed. */
function UMap({ d }: { d: Detail }) {
  const { space, devices } = d;
  const cells: { cls: string; title: string }[] = [];
  const byU = new Map<number, RackDeviceRow>();
  for (const dev of devices) {
    if (dev.u_start == null) continue;
    for (let u = dev.u_start; u < dev.u_start + (dev.u_height || 1); u++) byU.set(u, dev);
  }
  for (let u = 1; u <= space.u_total; u++) {
    const dev = byU.get(u);
    if (!dev) { cells.push({ cls: 'free', title: `U${u} · free` }); continue; }
    const alarmed = (dev.active_alarms ?? 0) > 0 || dev.status === 'OFFLINE';
    cells.push({ cls: `${dev.category ?? 'other'}${alarmed ? ' alarmed' : ''}`,
                 title: `U${u} · ${dev.name} · ${humanise(dev.device_type)}${alarmed ? ' · needs attention' : ''}` });
  }
  const present = new Set(devices.map((x) => x.category ?? 'other'));
  return (
    <div>
      <div className="rk-umap" role="img" aria-label={`${space.u_used} of ${space.u_total} rack units in use`}>
        {cells.map((c, i) => <div key={i} className={`u ${c.cls}`} title={c.title} />)}
      </div>
      <div className="rk-umap-scale"><span>U1</span><span>U{space.u_total}</span></div>
      <div className="rk-umap-key">
        {['it', 'network', 'power', 'cooling', 'environment'].filter((c) => present.has(c)).map((c) => (
          <span key={c}><i className={`u ${c}`} style={{ background: `var(--cat-${c === 'it' ? 'it' : c === 'network' ? 'cap' : c === 'power' ? 'pwr' : c === 'cooling' ? 'cool' : 'vis'})` }} />{CAT_WORD[c]}</span>
        ))}
        <span><i style={{ background: 'var(--bg-inset)', border: '1px solid var(--border)' }} />free</span>
      </div>
    </div>
  );
}

// --------------------------------------------------------------- devices

function Devices({ rows }: { rows: RackDeviceRow[] }) {
  const navigate = useNavigate();
  return (
    <section>
      <h3>Devices <span className="lead">top of the rack first; side channels last</span></h3>
      <table className="rk-table">
        <thead>
          <tr>
            <th>U</th><th>Name</th><th>Type</th><th>State</th>
            <th className="num">Alarms</th><th className="num">Draw</th><th className="num">Intake</th><th>Seen</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="is-link" onClick={() => navigate(`/devices/${r.id}`)}>
              <td className="muted">{r.u_start != null ? (r.u_height && r.u_height > 1 ? `${r.u_start}–${r.u_start + r.u_height - 1}` : r.u_start) : '—'}</td>
              <td><Link to={`/devices/${r.id}`}>{r.name}</Link></td>
              <td className="muted">{humanise(r.device_type)}</td>
              <td><span className={`dl-state ${statusClass(r.status)}`}><span className="dot" aria-hidden />{r.status.toLowerCase().replace(/_/g, ' ')}</span></td>
              <td className={`num ${r.active_alarms ? statusClass(r.max_severity) : ''}`}>{r.active_alarms || ''}</td>
              <td className="num">{r.power_w != null ? `${Math.round(r.power_w)} W` : <span className="dv-none">—</span>}</td>
              <td className="num">{r.inlet_temp_c != null ? `${r.inlet_temp_c.toFixed(1)} °C` : <span className="dv-none">—</span>}</td>
              <td className="muted">{r.last_seen ? relativeTime(r.last_seen) : 'never'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

