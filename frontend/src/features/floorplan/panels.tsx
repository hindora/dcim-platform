import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type {
  FloorEquipment, FloorPlan, FloorRack, RoomKpi, ThermalRoom, ThermalUnit, TwinDevice,
} from '../../api/client';
import { humanise } from '../../lib/format';
import {
  COOLING_LAYERS, RACK_LAYERS, dewPoint, gridRef, rackMeanRh, rackTiers, unitReadings,
  type CoolingLayer, type Legend as LegendSpec, type RackLayer, type Visibility,
} from './layers';

/** The viewer's side panel and legend. Facts only: every figure here is one
 *  the estate pages also show, read from the same endpoints. */

const f1 = (v: number | null | undefined, unit = '') => (v == null ? '–' : `${v.toFixed(1)}${unit}`);
const f0 = (v: number | null | undefined, unit = '') => (v == null ? '–' : `${Math.round(v)}${unit}`);

export function Legend({ spec }: { spec: LegendSpec }) {
  return (
    <div className="vw-legend">
      <div className="vw-legend-title">{spec.title}{spec.unit && <span className="muted"> ({spec.unit})</span>}</div>
      {spec.bands.map((b) => (
        <div key={b.label} className="vw-legend-row">
          <span className="vw-swatch" style={{ background: b.color }} aria-hidden />{b.label}
        </div>
      ))}
      {spec.none && (
        <div className="vw-legend-row muted">
          <span className="vw-swatch" style={{ background: 'var(--band-none)' }} aria-hidden />{spec.none}
        </div>
      )}
    </div>
  );
}

function Section({ title, open = false, children }: { title: string; open?: boolean; children: ReactNode }) {
  return (
    <details className="vw-section" open={open}>
      <summary>{title}</summary>
      <div className="vw-section-body">{children}</div>
    </details>
  );
}

function Row({ k, v }: { k: string; v: ReactNode }) {
  return <div className="vw-row"><span className="k">{k}</span><span className="v">{v}</span></div>;
}

function Tiles({ items }: { items: { label: string; value: string }[] }) {
  return (
    <div className="vw-tiles">
      {items.map((t) => <div key={t.label} className="vw-tile"><b>{t.value}</b><span>{t.label}</span></div>)}
    </div>
  );
}

function Bar({ value, max, label, warn = 80, crit = 90 }: { value: number | null; max: number | null; label: string; warn?: number; crit?: number }) {
  const pct = value != null && max ? Math.min(100, (100 * value) / max) : null;
  const tone = pct == null ? 'var(--text-faint)' : pct >= crit ? 'var(--critical)' : pct >= warn ? 'var(--warn)' : 'var(--accent)';
  return (
    <div className="vw-bar">
      <div className="vw-bar-head"><b>{f1(value, ' kW')}</b><span>{pct == null ? (max ? '' : 'no rating') : `${pct.toFixed(0)} %`}</span></div>
      <div className="vw-bar-track"><div className="vw-bar-fill" style={{ width: `${pct ?? 0}%`, background: tone }} /></div>
      <div className="vw-bar-label">{label}{max ? ` / ${f1(max, ' kW')}` : ''}</div>
    </div>
  );
}

// ---------------------------------------------------------------- room

export function RoomPanel({ plan, devices, kpi, thermal, vents }: {
  plan: FloorPlan; devices: TwinDevice[]; kpi?: RoomKpi; thermal?: ThermalRoom; vents: number;
}) {
  const byRack = new Map<string, TwinDevice[]>();
  for (const d of devices) byRack.set(d.rack_id, [...(byRack.get(d.rack_id) ?? []), d]);
  const temps: number[] = [];
  const rhs: number[] = [];
  for (const r of plan.racks) {
    const ds = byRack.get(r.id) ?? [];
    for (const t of rackTiers(ds, r.u_height ?? 42)) if (t != null) temps.push(t);
    const rh = rackMeanRh(ds);
    if (rh != null) rhs.push(rh);
  }
  const avg = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
  const avgT = avg(temps), avgRh = avg(rhs);
  const crahs = thermal?.crah_units ?? [];
  const ratedKw = crahs.reduce((a, u) => a + (u.rated_kw ?? 0), 0);
  const dutyKw = crahs.reduce((a, u) => a + (u.duty_kw ?? 0), 0);
  const pdus = devices.filter((d) => d.device_type === 'pdu').length;
  const freeU = plan.racks.reduce((a, r) => a + (r.free_u ?? 0), 0);

  return (
    <div className="vw-panel-body">
      <div className="vw-kv"><Row k="Room" v={plan.room_name} /><Row k="Site" v={plan.datacenter_code ?? '–'} />
        <Row k="Level" v={plan.level ?? '–'} /></div>
      <Section title="Information" open>
        <Row k="Room size" v={`${plan.extent.width_m} × ${plan.extent.depth_m} m`} />
        <Row k="Racks" v={plan.racks.length} />
        <Row k="Free U" v={freeU} />
        <Row k="Air handlers" v={plan.equipment.filter((e) => e.device_type === 'crah' || e.device_type === 'crac').length} />
        <Row k="Floor vents" v={<>{vents}<span className="muted"> assumed</span></>} />
        <Row k="Rack PDUs" v={pdus} />
        <Row k="Devices" v={devices.length} />
        {plan.unpositioned_equipment.length > 0 && <Row k="Not placed" v={plan.unpositioned_equipment.length} />}
      </Section>
      <Section title="Environmental" open>
        <Row k="Avg rack temp" v={f1(avgT, ' °C')} />
        <Row k="Max rack temp" v={f1(temps.length ? Math.max(...temps) : null, ' °C')} />
        <Row k="Min rack temp" v={f1(temps.length ? Math.min(...temps) : null, ' °C')} />
        <Row k="Avg rack RH" v={f1(avgRh, ' %')} />
        <Row k="Avg dew point" v={f1(dewPoint(avgT, avgRh), ' °C')} />
        <Row k="Compliance" v={kpi?.environmental.compliance_pct != null ? `${kpi.environmental.compliance_pct.toFixed(1)} %` : '–'} />
        {kpi?.environmental.note && <p className="muted vw-note">{kpi.environmental.note}</p>}
      </Section>
      <Section title="Cooling">
        <Row k="Units running" v={`${crahs.filter((u) => u.running).length} / ${crahs.length}`} />
        <Row k="Utilisation" v={ratedKw ? `${f0(dutyKw)} / ${f0(ratedKw)} kW` : '–'} />
        <Row k="Active utilisation" v={ratedKw ? `${((100 * dutyKw) / ratedKw).toFixed(1)} %` : '–'} />
        <Row k="Avg supply" v={f1(avg(crahs.map((u) => u.supply_c).filter((x): x is number => x != null)), ' °C')} />
        <Row k="Avg return" v={f1(avg(crahs.map((u) => u.return_c).filter((x): x is number => x != null)), ' °C')} />
        {thermal && (thermal.units_high_supply > 0 || thermal.units_high_return > 0) && (
          <p className="muted vw-note">{thermal.units_high_supply} high supply · {thermal.units_high_return} high return</p>
        )}
      </Section>
      <Section title="Power">
        <Row k="Total" v={f1(kpi?.power.total_kw, ' kW')} />
        <Row k="IT (AC)" v={f1(kpi?.power.it_ac_kw, ' kW')} />
        <Row k="IT (DC)" v={f1(kpi?.power.it_dc_kw, ' kW')} />
        <Row k="Cooling" v={f1(kpi?.power.cooling_kw, ' kW')} />
        <Row k="Other" v={f1(kpi?.power.other_kw, ' kW')} />
        <Row k="PUE (in room)" v={kpi?.power.pue != null ? kpi.power.pue.toFixed(2) : '–'} />
      </Section>
    </div>
  );
}

// ---------------------------------------------------------------- rack

export function RackPanel({ rack, devices }: { rack: FloorRack; devices: TwinDevice[] }) {
  const [b, m, t] = rackTiers(devices, rack.u_height ?? 42);
  const temps = [b, m, t].filter((x): x is number => x != null);
  const temp = temps.length ? Math.max(...temps) : null;
  const rh = rackMeanRh(devices);
  const pdus = devices.filter((d) => d.device_type === 'pdu');
  const faults = devices.filter((d) => d.max_severity !== 'CLEAR' || d.status === 'OFFLINE');
  return (
    <div className="vw-panel-body">
      <div className="vw-kv">
        <Row k="Grid reference" v={gridRef(rack.x, rack.y)} />
        <Row k="Row" v={rack.row_name ?? '–'} />
        <Row k="Faces" v={rack.facing === 'N' ? 'north' : rack.facing === 'S' ? 'south' : '–'} />
        <Row k="ΔT bottom → top" v={b != null && t != null ? `${(t - b).toFixed(1)} K` : '–'} />
      </div>
      <Tiles items={[
        { label: 'TEMP', value: f1(temp, ' °C') },
        { label: 'RH', value: f1(rh, ' %') },
        { label: 'DEW POINT', value: f1(dewPoint(temp, rh), ' °C') },
      ]} />
      <Section title="Total power" open>
        <Bar value={rack.load_kw ?? null} max={rack.rated_power_kw ?? null} label="rack PDUs" />
      </Section>
      <Section title="Rack PDU loads" open={pdus.length > 0}>
        {pdus.length === 0 && <p className="muted vw-note">No rack PDU reporting.</p>}
        <div className="vw-tiles">
          {pdus.map((p) => (
            <div key={p.id} className="vw-tile"><b>{p.power_w != null ? `${(p.power_w / 1000).toFixed(2)} kW` : '–'}</b>
              <span>{p.name.split('-')[0]}</span></div>
          ))}
        </div>
      </Section>
      <Section title="Readings" open>
        <Row k="Inlet bottom" v={f1(b, ' °C')} />
        <Row k="Inlet middle" v={f1(m, ' °C')} />
        <Row k="Inlet top" v={f1(t, ' °C')} />
        <Row k="Devices" v={`${rack.device_count}${rack.offline_count ? ` · ${rack.offline_count} offline` : ''}`} />
        <Row k="Free" v={rack.free_u != null ? `${rack.free_u} of ${rack.u_height ?? 42} U` : '–'} />
      </Section>
      {faults.length > 0 && (
        <Section title={`Conditions (${faults.length})`} open>
          <ul className="vw-list">
            {faults.slice(0, 8).map((d) => (
              <li key={d.id}><Link to={`/devices/${d.id}`}>{d.name}</Link>
                <span className="muted"> · {d.status === 'OFFLINE' ? 'offline' : d.max_severity.toLowerCase()}</span></li>
            ))}
          </ul>
        </Section>
      )}
      <div className="vw-links">
        <Link to={`/racks/${rack.id}?from=floorplan`}>Rack elevation</Link>
      </div>
    </div>
  );
}

export function DevicePanel({ device, rack }: { device: TwinDevice; rack?: FloorRack }) {
  return (
    <div className="vw-panel-body">
      <div className="vw-kv">
        <Row k="Type" v={humanise(device.device_type)} />
        {rack && <Row k="Rack" v={rack.name} />}
        <Row k="Slot" v={device.u_start ? `U${device.u_start}${device.u_height > 1 ? `–${device.u_start + device.u_height - 1}` : ''}` : (device.mount ?? '–')} />
        <Row k="Status" v={`${device.status.toLowerCase()}${device.max_severity !== 'CLEAR' ? ` · ${device.max_severity.toLowerCase()}` : ''}`} />
      </div>
      <Tiles items={[
        { label: 'AIR', value: f1(device.temp_c, ' °C') },
        { label: 'RH', value: f1(device.rh_pct, ' %') },
        { label: 'POWER', value: device.power_w != null ? `${(device.power_w / 1000).toFixed(2)} kW` : '–' },
      ]} />
      <div className="vw-links"><Link to={`/devices/${device.id}`}>Device</Link>
        {rack && <Link to={`/racks/${rack.id}?from=floorplan`}>Rack elevation</Link>}</div>
    </div>
  );
}

export function UnitPanel({ unit, thermal }: { unit: FloorEquipment; thermal?: ThermalUnit }) {
  const r = unitReadings(unit, thermal);
  const air = unit.device_type === 'crah' || unit.device_type === 'crac';
  return (
    <div className="vw-panel-body">
      <div className="vw-kv">
        <Row k="Type" v={humanise(unit.device_type)} />
        <Row k="Grid reference" v={unit.x != null && unit.y != null ? gridRef(unit.x, unit.y) : '–'} />
        {unit.w_m != null && <Row k="Footprint" v={`${unit.w_m} × ${unit.d_m} × ${unit.h_m} m${unit.basis === 'class' ? ' (estimate)' : ''}`} />}
        <Row k="Status" v={`${unit.status.toLowerCase()}${unit.max_severity !== 'CLEAR' ? ` · ${unit.max_severity.toLowerCase()}` : ''}`} />
        {thermal?.state && thermal.state !== 'ok' && <Row k="Verdict" v={`${humanise(thermal.state)}${thermal.reason ? ` – ${thermal.reason}` : ''}`} />}
      </div>
      {air ? (
        <Tiles items={[
          { label: 'RETURN', value: f1(r.return_c, ' °C') },
          { label: 'SUPPLY', value: f1(r.supply_c, ' °C') },
          { label: 'SETPOINT', value: f1(thermal?.setpoint_c, ' °C') },
        ]} />
      ) : (
        <Tiles items={[
          { label: 'AIR', value: f1(unit.temp_c, ' °C') },
          { label: 'RH', value: f1(unit.rh_pct, ' %') },
          { label: 'POWER', value: unit.power_w != null ? `${(unit.power_w / 1000).toFixed(1)} kW` : '–' },
        ]} />
      )}
      {air && (
        <Section title="Cooling" open>
          <Bar value={thermal?.duty_kw ?? null} max={thermal?.rated_kw ?? null} label="delivered" warn={85} crit={95} />
          <Row k="Fan" v={f0(thermal?.fan_pct, ' %')} />
          <Row k="CHW valve" v={f0(thermal?.valve_pct, ' %')} />
          <Row k="ΔT" v={f1(thermal?.delta_t_k, ' K')} />
          <Row k="Power" v={unit.power_w != null ? `${(unit.power_w / 1000).toFixed(1)} kW` : '–'} />
        </Section>
      )}
      <div className="vw-links"><Link to={`/devices/${unit.id}`}>Device</Link></div>
    </div>
  );
}

// ---------------------------------------------------------------- filters

export function FiltersPanel({ rackLayer, coolingLayer, vis, onRack, onCooling, onVis }: {
  rackLayer: RackLayer; coolingLayer: CoolingLayer; vis: Visibility;
  onRack: (l: RackLayer) => void; onCooling: (l: CoolingLayer) => void; onVis: (v: Visibility) => void;
}) {
  const [open, setOpen] = useState<'racks' | 'cooling' | 'vis'>('racks');
  const VIS: { key: keyof Visibility; label: string }[] = [
    { key: 'devices', label: 'Devices in racks' }, { key: 'plant', label: 'Plant and panels' },
    { key: 'aisles', label: 'Aisle tint' }, { key: 'containment', label: 'Containment' },
    { key: 'vents', label: 'Vent tiles' }, { key: 'labels', label: 'Labels' },
  ];
  return (
    <div className="vw-panel-body">
      <details className="vw-section" open={open === 'racks'} onToggle={(e) => (e.currentTarget.open ? setOpen('racks') : null)}>
        <summary>Racks</summary>
        <div className="vw-section-body vw-radios" role="radiogroup" aria-label="Rack layer">
          {RACK_LAYERS.map((l) => (
            <label key={l.key} className={l.key === rackLayer ? 'is-on' : undefined}>
              <input type="radio" name="rack-layer" checked={l.key === rackLayer} onChange={() => onRack(l.key)} />
              <span>{l.label}</span>{l.hint && <small>{l.hint}</small>}
            </label>
          ))}
        </div>
      </details>
      <details className="vw-section" open={open === 'cooling'} onToggle={(e) => (e.currentTarget.open ? setOpen('cooling') : null)}>
        <summary>Cooling</summary>
        <div className="vw-section-body vw-radios" role="radiogroup" aria-label="Cooling layer">
          {COOLING_LAYERS.map((l) => (
            <label key={l.key} className={l.key === coolingLayer ? 'is-on' : undefined}>
              <input type="radio" name="cooling-layer" checked={l.key === coolingLayer} onChange={() => onCooling(l.key)} />
              <span>{l.label}</span>{l.hint && <small>{l.hint}</small>}
            </label>
          ))}
        </div>
      </details>
      <details className="vw-section" open={open === 'vis'} onToggle={(e) => (e.currentTarget.open ? setOpen('vis') : null)}>
        <summary>Visibility</summary>
        <div className="vw-section-body vw-radios">
          {VIS.map((v) => (
            <label key={v.key}>
              <input type="checkbox" checked={vis[v.key]} onChange={(e) => onVis({ ...vis, [v.key]: e.target.checked })} />
              <span>{v.label}</span>
            </label>
          ))}
        </div>
      </details>
    </div>
  );
}
