import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { api, ApiError, type Alarm } from '../../api/client';
import type {
  FloorEquipment, FloorPlan, FloorRack, RoomKpi, ThermalField, ThermalRoom, ThermalUnit, TwinDevice, TwinRackIndex,
  TwinRoomIndices,
} from '../../api/client';
import { humanise } from '../../lib/format';
import {
  COOLING_LAYERS, HEAT_PLANES, RACK_LAYERS, dewPoint, gridRef, pickExhaust, rackMeanRh, rackTiers, unitReadings,
  type CoolingLayer, type HeatPlane, type Legend as LegendSpec, type RackLayer, type Visibility,
} from './layers';
import type { PathKind, PathOverlay } from './paths';
import { fmtDT, fmtT, legendInUnit, numT, unitLabel } from './units';

/** The viewer's side panel and legend. Facts only: every figure here is one
 *  the estate pages also show, read from the same endpoints. */

const f1 = (v: number | null | undefined, unit = '') => (v == null ? '–' : `${v.toFixed(1)}${unit}`);
const f0 = (v: number | null | undefined, unit = '') => (v == null ? '–' : `${Math.round(v)}${unit}`);
const f2 = (v: number | null | undefined) => (v == null ? '–' : v.toFixed(2));

/** Herrlin's RCI rating as a tone, so a 90 % reads as the problem it is. */
function ratingTone(r: string | null | undefined): string {
  return r === 'good' ? 'var(--ok)' : r === 'acceptable' ? 'var(--warn)' : r === 'poor' ? 'var(--critical)' : 'inherit';
}

/** RTI's verdict: Herrlin reads 100 % as balanced, above it as exhaust
 *  recirculating into intakes, below it as supply bypassing the kit. */
function rtiVerdict(v: number | null | undefined): string | null {
  if (v == null) return null;
  if (v > 110) return 'recirculation';
  if (v < 90) return 'bypass';
  return 'balanced';
}

export function Legend({ spec: raw }: { spec: LegendSpec }) {
  const spec = legendInUnit(raw);
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

export function RoomPanel({ plan, devices, kpi, thermal, vents, indices, field }: {
  plan: FloorPlan; devices: TwinDevice[]; kpi?: RoomKpi; thermal?: ThermalRoom; vents: number;
  indices?: TwinRoomIndices | null; field?: ThermalField | null;
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
        <Row k="Avg rack temp" v={fmtT(avgT)} />
        <Row k="Max rack temp" v={fmtT(temps.length ? Math.max(...temps) : null)} />
        <Row k="Min rack temp" v={fmtT(temps.length ? Math.min(...temps) : null)} />
        <Row k="Avg rack RH" v={f1(avgRh, ' %')} />
        <Row k="Avg dew point" v={fmtT(dewPoint(avgT, avgRh))} />
        <Row k="Compliance" v={kpi?.environmental.compliance_pct != null ? `${kpi.environmental.compliance_pct.toFixed(1)} %` : '–'} />
        {kpi?.environmental.note && <p className="muted vw-note">{kpi.environmental.note}</p>}
      </Section>
      {indices && <IndicesSection ix={indices} field={field} />}
      <Section title="Cooling">
        <Row k="Units running" v={`${crahs.filter((u) => u.running).length} / ${crahs.length}`} />
        <Row k="Utilisation" v={ratedKw ? `${f0(dutyKw)} / ${f0(ratedKw)} kW` : '–'} />
        <Row k="Active utilisation" v={ratedKw ? `${((100 * dutyKw) / ratedKw).toFixed(1)} %` : '–'} />
        <Row k="Avg supply" v={fmtT(avg(crahs.map((u) => u.supply_c).filter((x): x is number => x != null)))} />
        <Row k="Avg return" v={fmtT(avg(crahs.map((u) => u.return_c).filter((x): x is number => x != null)))} />
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

/** The room's indices (docs/27 D6), each with what it was scored from.
 *  RCI and RTI are Herrlin's, SHI/RHI Sharma's; the counts are shown so a
 *  perfect score from two sensors reads as exactly that. */
function IndicesSection({ ix, field }: { ix: TwinRoomIndices; field?: ThermalField | null }) {
  const verdict = rtiVerdict(ix.rti);
  return (
    <Section title={`ASHRAE ${ix.ashrae_class} · indices`} open>
      <div className="vw-tiles">
        <div className="vw-tile" style={{ color: ratingTone(ix.rci_rating) }}>
          <b>{f0(ix.rci_hi, ' %')}</b><span>RCI HI</span></div>
        <div className="vw-tile"><b>{f0(ix.rci_lo, ' %')}</b><span>RCI LO</span></div>
        <div className="vw-tile" style={{ color: verdict && verdict !== 'balanced' ? 'var(--warn)' : 'inherit' }}>
          <b>{f0(ix.rti, ' %')}</b><span>RTI</span></div>
      </div>
      {ix.rci_rating && (
        <p className="vw-note" style={{ color: ratingTone(ix.rci_rating) }}>
          Rack cooling {ix.rci_rating}{verdict ? ` · return air ${verdict}` : ''}
        </p>
      )}
      <Row k="SHI / RHI" v={ix.shi != null ? `${f2(ix.shi)} / ${f2(ix.rhi)}` : '–'} />
      <Row k="Hot spots" v={ix.hot_spots} />
      <Row k="Reference supply" v={fmtT(ix.supply_ref_c)} />
      <Row k="Reference return" v={fmtT(ix.return_ref_c)} />
      <Row k="Equipment ΔT" v={fmtDT(ix.dt_equip_k)} />
      <Row k="Intakes · exhausts" v={`${ix.intakes} · ${ix.exhausts}`} />
      <Row k="Air handlers in reference" v={ix.units_in_ref} />
      <p className="muted vw-note">
        RCI over every intake against the {ix.ashrae_class} band; RTI from the air handlers' rise against the
        power-weighted rack rise{ix.unweighted ? ` (${ix.unweighted} unweighted, no power reading)` : ''}.
      </p>
      {field && (
        <p className="muted vw-note">
          Heat map from {field.intake_points} intake and {field.exhaust_points} exhaust face points, per aisle;
          faded where no sensor reaches.{field.note ? ` ${field.note}` : ''}
        </p>
      )}
    </Section>
  );
}

// ---------------------------------------------------------------- conditions

const SEV_RANK: Record<string, number> = { CRITICAL: 5, MAJOR: 4, MINOR: 3, WARNING: 2, INFO: 1 };

/** The open conditions on one device, worst first, each with the two actions
 *  an operator takes from where they are standing: acknowledge (I have it)
 *  and clear (it is resolved and the source will not say so). Suppression is
 *  not offered here - holding alarms back is planned work, so it goes through
 *  a maintenance window, which is time-boxed and audited. */
export function UnitAlarms({ deviceId, deviceName }: { deviceId: string; deviceName: string }) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const q = useQuery<{ items: Alarm[] }>({
    queryKey: ['alarms', 'device', deviceId],
    queryFn: () => api.alarms({ device_id: deviceId, include_symptoms: 'true', limit: '50' }),
    refetchInterval: 30_000,
  });
  const refresh = () => {
    for (const key of [['alarms'], ['alarm'], ['alarm-summary'], ['twin-scene'], ['estate-alarms'], ['dashboard']]) {
      qc.invalidateQueries({ queryKey: key });
    }
  };
  const fail = (e: unknown) => setError(
    e instanceof ApiError && e.status === 409 ? 'Someone else got there first; the list has been refreshed.'
      : e instanceof ApiError && e.status === 403 ? 'Acknowledging and clearing need the operator role.'
      : `That did not go through: ${String(e instanceof Error ? e.message : e)}`);
  const ack = useMutation({ mutationFn: (id: string) => api.acknowledgeAlarm(id),
                            onSuccess: () => { setError(null); refresh(); }, onError: (e) => { fail(e); refresh(); } });
  const clear = useMutation({ mutationFn: (id: string) => api.clearAlarm(id),
                              onSuccess: () => { setError(null); setConfirming(null); refresh(); },
                              onError: (e) => { fail(e); setConfirming(null); refresh(); } });

  const items = [...(q.data?.items ?? [])].sort((a, b) =>
    (SEV_RANK[b.severity] ?? 0) - (SEV_RANK[a.severity] ?? 0) || b.last_seen.localeCompare(a.last_seen));
  const open = items.filter((a) => a.state === 'ACTIVE').length;
  const title = q.isLoading ? 'Conditions' : items.length ? `Conditions (${items.length}${open ? `, ${open} new` : ''})` : 'Conditions';

  return (
    <Section title={title} open={items.length > 0}>
      {q.isError && <p className="muted vw-note">The alarm list could not be read.</p>}
      {!q.isLoading && !q.isError && items.length === 0 && (
        <p className="muted vw-note">Nothing open on {deviceName}.</p>
      )}
      {error && <p className="vw-note vw-bad" role="alert">{error}</p>}
      <ul className="vw-alarms">
        {items.map((a) => (
          <li key={a.id} className={`vw-alarm sev-${a.severity.toLowerCase()}${a.is_symptom ? ' is-symptom' : ''}`}>
            <div className="vw-alarm-head">
              <span className="vw-sev">{a.severity.toLowerCase()}</span>
              <Link to={`/alarms/${a.id}`} className="vw-alarm-type">{humanise(a.alarm_type)}</Link>
              {a.instance && <span className="muted">{a.instance}</span>}
            </div>
            <p className="vw-alarm-msg">{a.message}</p>
            <div className="vw-alarm-meta muted">
              {a.state === 'ACKNOWLEDGED' ? 'Acknowledged' : 'New'}
              {a.is_symptom ? ' · explained by a root cause' : ''}
              {` · since ${new Date(a.first_seen).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })}`}
            </div>
            {confirming === a.id ? (
              <div className="vw-alarm-acts" role="group" aria-label="Confirm clear">
                <span className="vw-note">Clear by hand? It reopens if the condition is still there.</span>
                <button type="button" className="vw-icon is-danger" disabled={clear.isPending}
                        onClick={() => clear.mutate(a.id)}>{clear.isPending ? 'Clearing…' : 'Clear it'}</button>
                <button type="button" className="vw-icon" onClick={() => setConfirming(null)}>Keep it</button>
              </div>
            ) : (
              <div className="vw-alarm-acts">
                {a.state === 'ACTIVE' && (
                  <button type="button" className="vw-icon" disabled={ack.isPending && ack.variables === a.id}
                          onClick={() => ack.mutate(a.id)}>
                    {ack.isPending && ack.variables === a.id ? 'Acknowledging…' : 'Acknowledge'}
                  </button>
                )}
                <button type="button" className="vw-icon" onClick={() => setConfirming(a.id)}>Clear</button>
              </div>
            )}
          </li>
        ))}
      </ul>
      <div className="vw-links">
        <Link to={`/maintenance?schedule=${deviceId}`}>Plan maintenance on this unit</Link>
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------- power

/** The rack's power three ways against what one feed can carry: allocated
 *  (budget of what is installed) and reserved (held for planned work) as the
 *  wide bar, measured as the thin bar through it. A bullet chart: the plan is
 *  the body, the meter is the reading, the rating is the track. */
function PowerBullet({ rack }: { rack: FloorRack }) {
  const cap = rack.rated_power_kw ?? null;
  const alloc = rack.allocated_kw ?? null;
  const held = rack.reserved_kw ?? null;
  const meas = rack.load_kw ?? null;
  const committed = (alloc ?? 0) + (held ?? 0);
  // Scale to the larger of the rating and anything over it, so an
  // over-committed rack shows its excess instead of clipping.
  const scale = Math.max(cap ?? 0, committed, meas ?? 0) || 1;
  const w = (v: number | null) => `${Math.max(0, (100 * (v ?? 0)) / scale)}%`;
  const pct = (v: number | null) => (cap && v != null ? ` · ${Math.round((100 * v) / cap)} %` : '');
  const over = cap != null && committed > cap ? committed - cap : null;
  const derated = rack.allocated_derated ?? 0;
  const unrated = rack.allocated_unrated ?? 0;
  const summary = cap == null
    ? 'No rating recorded for this rack, so nothing is measured against it.'
    : `${f1(committed)} of ${f1(cap)} kW committed; ${f1(meas)} kW measured.`;
  return (
    <div className="vw-bullet">
      <div className="vw-bullet-track" role="img" aria-label={summary}>
        {cap != null && <div className="vw-bullet-cap" style={{ width: w(cap) }} />}
        <div className="vw-bullet-alloc" style={{ width: w(alloc) }} />
        <div className="vw-bullet-held" style={{ left: w(alloc), width: w(held) }} />
        {meas != null && <div className="vw-bullet-meas" style={{ width: w(meas) }} />}
        {cap != null && [0.8, 0.9].map((f) => (
          <span key={f} className="vw-bullet-tick" style={{ left: w(cap * f) }} aria-hidden />
        ))}
      </div>
      <dl className="vw-bullet-key">
        <dt><span className="sw alloc" aria-hidden />Allocated</dt><dd>{f1(alloc, ' kW')}{pct(alloc)}</dd>
        <dt><span className="sw held" aria-hidden />Reserved</dt><dd>{f1(held, ' kW')}{pct(held)}</dd>
        <dt><span className="sw meas" aria-hidden />Measured</dt><dd>{f1(meas, ' kW')}{pct(meas)}</dd>
        <dt><span className="sw cap" aria-hidden />Rating, one feed</dt><dd>{f1(cap, ' kW')}</dd>
      </dl>
      {over != null && <p className="vw-note vw-bullet-over">Committed past the rating by {f1(over, ' kW')}.</p>}
      {(derated > 0 || unrated > 0) && (
        <p className="muted vw-note">
          {derated > 0 && `${derated} device${derated === 1 ? '' : 's'} budgeted at 60 % of nameplate. `}
          {unrated > 0 && `${unrated} with no rating, counted as zero.`}
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- rack

export function RackPanel({ rack, devices, index }: { rack: FloorRack; devices: TwinDevice[]; index?: TwinRackIndex | null }) {
  const [b, m, t] = rackTiers(devices, rack.u_height ?? 42);
  const [eb, em, et] = rackTiers(devices, rack.u_height ?? 42, pickExhaust);
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
        <Row k="ΔT bottom → top" v={b != null && t != null ? fmtDT(t - b) : '–'} />
      </div>
      <Tiles items={[
        { label: 'TEMP', value: fmtT(temp) },
        { label: 'RH', value: f1(rh, ' %') },
        { label: 'DEW POINT', value: fmtT(dewPoint(temp, rh)) },
      ]} />
      {index && (
        <>
          {index.hot && (
            <p className="vw-note" style={{ color: index.hot === 'out' ? 'var(--critical)' : 'var(--warn)' }}>
              Hot spot · intake {index.hot === 'out' ? 'outside the allowable band' : 'above the recommended ceiling'}
            </p>
          )}
          <Tiles items={[
            { label: 'INTAKE MAX', value: fmtT(index.inlet_max_c) },
            { label: 'EXHAUST MAX', value: fmtT(index.exhaust_max_c) },
            { label: 'SPREAD', value: fmtDT(index.spread_k) },
          ]} />
          <Section title="Indices" open>
            <Row k="SHI / RHI" v={index.shi != null ? `${f2(index.shi)} / ${f2(index.rhi)}` : '–'} />
            <Row k="Rise in → out" v={fmtDT(index.rise_k)} />
            <Row k="Intakes · exhausts" v={`${index.intakes} · ${index.exhausts}`} />
            <p className="muted vw-note">SHI is the share of this rack's heat its intake picked up before entering
              (Sharma); 0 means it breathed pure supply air.</p>
          </Section>
        </>
      )}
      <Section title="Power" open>
        <PowerBullet rack={rack} />
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
        <Row k="Inlet bottom" v={fmtT(b)} />
        <Row k="Inlet middle" v={fmtT(m)} />
        <Row k="Inlet top" v={fmtT(t)} />
        {(eb != null || et != null) && <Row k="Exhaust bottom / mid / top" v={`${numT(eb)} / ${numT(em)} / ${numT(et)} ${unitLabel()}`} />}
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
        { label: 'AIR', value: fmtT(device.temp_c) },
        { label: device.exhaust_c != null ? 'EXHAUST' : 'RH', value: device.exhaust_c != null ? fmtT(device.exhaust_c) : f1(device.rh_pct, ' %') },
        { label: 'POWER', value: device.power_w != null ? `${(device.power_w / 1000).toFixed(2)} kW` : '–' },
      ]} />
      {device.exhaust_c != null && device.temp_c != null && (
        <div className="vw-kv"><Row k="Rise in → out" v={fmtDT(device.exhaust_c - device.temp_c)} /></div>
      )}
      <UnitAlarms deviceId={device.id} deviceName={device.name} />
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
          { label: 'RETURN', value: fmtT(r.return_c) },
          { label: 'SUPPLY', value: fmtT(r.supply_c) },
          { label: 'SETPOINT', value: fmtT(thermal?.setpoint_c) },
        ]} />
      ) : (
        <Tiles items={[
          { label: 'AIR', value: fmtT(unit.temp_c) },
          { label: 'RH', value: f1(unit.rh_pct, ' %') },
          { label: 'POWER', value: unit.power_w != null ? `${(unit.power_w / 1000).toFixed(1)} kW` : '–' },
        ]} />
      )}
      {air && (
        <Section title="Cooling" open>
          <Bar value={thermal?.duty_kw ?? null} max={thermal?.rated_kw ?? null} label="delivered" warn={85} crit={95} />
          <Row k="Fan" v={f0(thermal?.fan_pct, ' %')} />
          <Row k="CHW valve" v={f0(thermal?.valve_pct, ' %')} />
          <Row k="ΔT" v={fmtDT(thermal?.delta_t_k)} />
          <Row k="Power" v={unit.power_w != null ? `${(unit.power_w / 1000).toFixed(1)} kW` : '–'} />
        </Section>
      )}
      <UnitAlarms deviceId={unit.id} deviceName={unit.name} />
      <div className="vw-links"><Link to={`/devices/${unit.id}`}>Device</Link></div>
    </div>
  );
}

// ---------------------------------------------------------------- filters

export function FiltersPanel({ rackLayer, coolingLayer, heat, vis, onRack, onCooling, onHeat, onVis }: {
  rackLayer: RackLayer; coolingLayer: CoolingLayer; heat: HeatPlane; vis: Visibility;
  onRack: (l: RackLayer) => void; onCooling: (l: CoolingLayer) => void; onHeat: (h: HeatPlane) => void;
  onVis: (v: Visibility) => void;
}) {
  const [open, setOpen] = useState<'racks' | 'cooling' | 'heat' | 'vis'>('racks');
  const VIS: { key: keyof Visibility; label: string }[] = [
    { key: 'devices', label: 'Devices in racks' }, { key: 'plant', label: 'Plant and panels' },
    { key: 'aisles', label: 'Aisle tint' }, { key: 'containment', label: 'Containment' },
    { key: 'vents', label: 'Vent tiles' }, { key: 'hotspots', label: 'Hot-spot markers' },
    { key: 'faces', label: 'See into racks' },
    { key: 'labels', label: 'Labels' },
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
      <details className="vw-section" open={open === 'heat'} onToggle={(e) => (e.currentTarget.open ? setOpen('heat') : null)}>
        <summary>Heat map</summary>
        <div className="vw-section-body vw-radios" role="radiogroup" aria-label="Heat map plane">
          {HEAT_PLANES.map((l) => (
            <label key={l.key} className={l.key === heat ? 'is-on' : undefined}>
              <input type="radio" name="heat-plane" checked={l.key === heat} onChange={() => onHeat(l.key)} />
              <span>{l.label}</span>{l.hint && <small>{l.hint}</small>}
            </label>
          ))}
          <p className="muted vw-note">The air at that height, interpolated along each aisle from the rack faces that
            open onto it - cold aisles from intakes, hot aisles from exhausts, never across a row. Faded where no
            sensor reaches.</p>
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

// ---------------------------------------------------------------- paths

/** Power path, cooling path or impact for the selection. The picture draws
 *  what stands in this room; the list here names every hop, with the room of
 *  anything that stands elsewhere. */
export function PathsPanel({ kind, overlay, loading, error, canImpact, inRoom, onPick, onClear }: {
  kind: PathKind | null; overlay: PathOverlay | null; loading: boolean; error: boolean; canImpact: boolean;
  inRoom: (id: string) => boolean; onPick: (k: PathKind) => void; onClear: () => void;
}) {
  const btn = (k: PathKind, label: string, disabled = false) => (
    <button key={k} type="button" className={`vw-icon${kind === k ? ' is-on' : ''}`} disabled={disabled}
            onClick={() => (kind === k ? onClear() : onPick(k))}>{label}</button>
  );
  return (
    <Section title="Paths" open>
      <div className="vw-btns">
        {btn('power', 'POWER')}{btn('cooling', 'COOLING')}{btn('impact', 'IMPACT', !canImpact)}
      </div>
      {loading && <p className="muted vw-note">Tracing…</p>}
      {error && <p className="muted vw-note">The topology service could not answer for this device.</p>}
      {overlay && overlay.kind !== 'impact' && (
        <div className="vw-chains">
          {overlay.chains.length === 0 && <p className="muted vw-note">No path recorded on this layer.</p>}
          {overlay.chains.map((c, i) => (
            <div key={i} className="vw-chain">
              <div className="vw-chain-head">
                <b>{c.side ? `Side ${c.side}` : 'Path'}</b>
                <span className={`muted${c.verdict !== 'complete' ? ' vw-bad' : ''}`}>{c.verdict}</span>
              </div>
              <ol>
                {c.nodes.map((n, j) => (
                  <li key={`${n.id}-${j}`} className={inRoom(n.id) ? undefined : 'is-away'}>
                    <Link to={`/devices/${n.id}`}>{n.name}</Link>
                    {!inRoom(n.id) && n.room_name && <span className="muted"> · {n.room_name}</span>}
                  </li>
                ))}
              </ol>
            </div>
          ))}
        </div>
      )}
      {overlay && overlay.kind === 'impact' && (
        <div className="vw-chains">
          <Row k="Cut off" v={overlay.cutOff.length} />
          <Row k="Degraded" v={overlay.degraded.length} />
          {overlay.cutOff.length > 0 && (
            <ul className="vw-list">
              {overlay.cutOff.slice(0, 12).map((n) => (
                <li key={n.id}><Link to={`/devices/${n.id}`}>{n.name}</Link>
                  <span className="muted"> · cut off{!inRoom(n.id) && n.room_name ? ` · ${n.room_name}` : ''}</span></li>
              ))}
              {overlay.cutOff.length > 12 && <li className="muted">and {overlay.cutOff.length - 12} more</li>}
            </ul>
          )}
          {overlay.degraded.length > 0 && (
            <ul className="vw-list">
              {overlay.degraded.slice(0, 8).map((n) => (
                <li key={n.id}><Link to={`/devices/${n.id}`}>{n.name}</Link>
                  <span className="muted"> · one side left{!inRoom(n.id) && n.room_name ? ` · ${n.room_name}` : ''}</span></li>
              ))}
            </ul>
          )}
        </div>
      )}
      {overlay?.notes.map((n) => <p key={n} className="muted vw-note">{n}</p>)}
    </Section>
  );
}
