import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import {
  api, atLeast, ApiError, type Alarm, type DeviceDetail, type DeviceState, type Impact, type MetricValue, type NetworkInterface,
  type PowerChain, type SiteKpi, type TwinRoom, type TwinSiteScene,
} from '../../api/client';
import type {
  HistoryOut,
  FloorEquipment, FloorPlan, FloorRack, RoomKpi, ThermalField, ThermalRoom, ThermalUnit, TwinDevice, TwinRackIndex,
  TwinRoomIndices,
} from '../../api/client';
import { formatMetric, formatSpeed, humanise, metricLabel, relativeTime } from '../../lib/format';
import { METRICS, type MetricKey } from '../../lib/metrics.gen';
import { chartGroupsFor } from '../devices/chartGroups';
import {
  COOLING_LAYERS, HEAT_PLANES, RACK_LAYERS, dewPoint, gridRef, pickExhaust, rackMeanRh, rackTiers, unitReadings,
  type CoolingLayer, type HeatPlane, type Legend as LegendSpec, type RackLayer, type Visibility,
} from './layers';
import type { PathKind, PathOverlay } from './paths';
import { fmtDT, fmtT, legendInUnit, numT, unitLabel } from './units';
import { alarmColor } from './colors';
import { glyphFor, TileGlyph, type GlyphName } from './glyphs';
import { Sparkline } from '../../components/Sparkline';
import { BUILDING_LAYERS, NO_READING, bandColor, roomLayerBands, roomLayerText, roomLayerValue, type BuildingLayer } from './layers';

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

const LEGEND_KEY = 'dcim.viewer.legend:';

/** A legend is a chip that opens to its bands. Open or closed is remembered
 *  per legend in this browser; a narrow window starts closed. */
export function Legend({ spec: raw }: { spec: LegendSpec }) {
  const spec = legendInUnit(raw);
  const [open, setOpen] = useState<boolean>(() => {
    try {
      const v = localStorage.getItem(LEGEND_KEY + raw.title);
      if (v === 'open') return true;
      if (v === 'closed') return false;
    } catch { /* a private window */ }
    return typeof window === 'undefined' ? true : window.innerWidth >= 1200;
  });
  const toggle = (next: boolean) => {
    setOpen(next);
    try { localStorage.setItem(LEGEND_KEY + raw.title, next ? 'open' : 'closed'); } catch { /* a private window */ }
  };
  return (
    <details className="vw-legend" open={open} onToggle={(e) => toggle((e.currentTarget as HTMLDetailsElement).open)}>
      <summary className="vw-legend-chip">
        <span className="vw-legend-title">{spec.title}{spec.unit && <span className="muted"> ({spec.unit})</span>}</span>
        <span className="chev" aria-hidden />
      </summary>
      <div className="vw-legend-body">
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
    </details>
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

function Row({ k, v }: { k: ReactNode; v: ReactNode }) {
  return <div className="vw-row"><span className="k">{k}</span><span className="v">{v}</span></div>;
}

/** The headline readings as tiles: a glyph for the kind of reading, the
 *  value, the label. The glyph comes from the label (glyphs.tsx), or is named
 *  outright for a tile whose label is not in that table. */
function Tiles({ items, spark }: {
  items: { label: string; value: string; icon?: GlyphName; metric?: string }[];
  /** The last hour of each tile's metric, by key; a tile without one draws
   *  no line rather than a placeholder. */
  spark?: Record<string, number[][]>;
}) {
  return (
    <div className="vw-tiles">
      {items.map((t) => {
        const g = t.icon ?? glyphFor(t.label);
        const pts = t.metric ? spark?.[t.metric] : undefined;
        const unit = t.metric ? METRICS[t.metric as MetricKey]?.unit ?? '' : '';
        return (
          <div key={t.label} className="vw-tile">
            {g ? <TileGlyph name={g} /> : <span className="vw-glyph" aria-hidden />}
            <b>{t.value}</b><span>{t.label}</span>
            {pts && pts.length > 1 && (
              <Sparkline points={pts} unit={unit} floor={SPARK_FLOOR[unit] ?? 1} height={18} className="vw-spark" />
            )}
          </div>
        );
      })}
    </div>
  );
}

/** The least a sparkline's axis may span, per unit, so a probe's normal
 *  wobble reads as a flat line and not as weather. */
const SPARK_FLOOR: Record<string, number> = {
  C: 2, '\u00b0C': 2, pct: 5, '%': 5, W: 50, kW: 0.1, A: 0.5, V: 2, 'L/s': 0.2, 'l/s': 0.2,
};

/** What each class's tiles chart: the keys its Tiles name as `metric`. */
const SPARK_METRICS: Record<DeviceClass, string[]> = {
  server: ['power_draw', 'inlet_temperature', 'cpu_temperature', 'exhaust_temperature'],
  network: ['cpu_utilization', 'memory_utilization', 'cpu_temperature'],
  pdu: ['load_pct', 'current', 'power_draw'],
  sensor: ['ambient_temperature', 'relative_humidity'],
  cdu: ['water_supply_temp', 'water_return_temp', 'water_flow', 'power_draw'],
  other: ['power_draw'],
};

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
/** The building rung's panel (docs/27 §8b): the site's numbers, then its rooms by level. */
/** A room's swatch and figure under a building layer, the same colour the
 *  scene paints it so the list and the picture agree. */
function roomSwatch(r: TwinRoom, layer: BuildingLayer): string {
  if (layer === 'alarm') return alarmColor(r.max_severity, 0);
  const v = roomLayerValue(r, layer);
  return v == null ? NO_READING : bandColor(roomLayerBands(layer)!, v);
}
function roomFigure(r: TwinRoom, layer: BuildingLayer): string {
  const v = roomLayerValue(r, layer);
  return v == null ? '–' : `${v.toFixed(0)} %`;
}

export function BuildingPanel({ scene, kpi, hover, onHover, onEnter, rung = 'building', onEnterBuilding, layer = 'alarm' }: {
  scene: TwinSiteScene; kpi?: SiteKpi; hover: string | null; layer?: BuildingLayer;
  onHover: (id: string | null) => void; onEnter: (id: string) => void;
  /** At the site rung the panel leads with the building and the compound. */
  rung?: 'site' | 'building'; onEnterBuilding?: () => void;
}) {
  const levels = [...scene.levels].sort((a, b) => b.ordinal - a.ordinal);
  const outdoorRooms = scene.rooms.filter((r) => (r.level ?? '').toLowerCase() === 'site');
  const zones = (scene.compound?.zones ?? []).filter((z) => !z.room_id && z.kind !== 'fence');
  const word = (k: string) => k.replace(/_/g, ' ');
  const racks = scene.rooms.reduce((a, r) => a + r.rack_count, 0);
  const devices = scene.rooms.reduce((a, r) => a + r.device_count, 0);
  const pct = (v: number | null | undefined, note?: string | null) =>
    v == null ? <span className="muted" title={note ?? undefined}>–</span> : `${Math.round(v)} %`;
  const w = kpi?.weather;
  const unplaced = scene.rooms.filter((r) => r.origin_x_m == null || r.origin_y_m == null || !r.width_m || !r.depth_m);
  return (
    <div className="vw-panel-body">
      <div className="vw-kv"><Row k="Site" v={scene.name && scene.name !== scene.code ? `${scene.code} · ${scene.name}` : scene.code} />
        <Row k="Levels" v={scene.levels.length} /><Row k="Rooms" v={scene.rooms.length} />
        <Row k="Racks" v={racks} /><Row k="Devices" v={devices} /></div>
      <Section title="Site" open>
        <Row k="PUE" v={kpi?.efficiency?.pue?.value != null ? kpi.efficiency.pue.value.toFixed(2)
          : <span className="muted" title={kpi?.efficiency?.pue?.note ?? undefined}>–</span>} />
        <Row k="IT load" v={kpi?.power?.it_load_kw != null ? `${kpi.power.it_load_kw.toFixed(0)} kW` : '–'} />
        <Row k="Power used" v={pct(kpi?.utilisation?.power?.pct, kpi?.utilisation?.power?.note)} />
        <Row k="Space used" v={pct(kpi?.utilisation?.space?.pct, kpi?.utilisation?.space?.note)} />
        <Row k="Cooling used" v={pct(kpi?.utilisation?.cooling?.pct, kpi?.utilisation?.cooling?.note)} />
        <Row k="Outdoor air" v={w?.available && w.dry_bulb_c != null
          ? <>{fmtT(w.dry_bulb_c)}{w.humidity_pct != null && <span className="muted"> · {Math.round(w.humidity_pct)} % RH</span>}</>
          : <span className="muted">{w?.note ?? '–'}</span>} />
      </Section>
      {rung === 'site' && (
        <Section title="Compound" open>
          <div className="bld-rooms">
            <ul>
              <li>
                <button type="button" className={hover === '__building__' ? 'is-current' : undefined} onClick={onEnterBuilding}
                        onMouseEnter={() => onHover('__building__')} onMouseLeave={() => onHover(null)}>
                  <span className="swatch" style={{ background: 'var(--text-muted)' }} aria-hidden />
                  <span className="name">{scene.name}</span>
                  <span className="muted">{scene.levels.length} levels</span>
                  <span className="count">{scene.rooms.filter((r) => (r.level ?? '').toLowerCase() !== 'site').length} rooms</span>
                </button>
              </li>
              {outdoorRooms.map((r) => (
                <li key={r.id}>
                  <button type="button" className={hover === r.id ? 'is-current' : undefined}
                          onClick={() => onEnter(r.id)} onMouseEnter={() => onHover(r.id)} onMouseLeave={() => onHover(null)}>
                    <span className="swatch" style={{ background: alarmColor(r.max_severity, 0) }} aria-hidden />
                    <span className="name">{r.name}</span>
                    <span className="muted">outdoor</span>
                    <span className="count">{r.device_count}</span>
                  </button>
                </li>
              ))}
            </ul>
            {zones.length > 0 && (
              <ul className="bld-zones" aria-label="Drawing only">
                {zones.map((z) => <li key={z.id}><span className="name">{z.name ?? word(z.kind)}</span><span className="muted">{word(z.kind)}</span></li>)}
              </ul>
            )}
            {zones.length === 0 && outdoorRooms.length === 0 && (
              <p className="muted bld-note">No compound in this import yet: the site is its building until the simulator's next export.</p>
            )}
          </div>
        </Section>
      )}
      <Section title="Rooms" open={rung === 'building'}>
        <div className="bld-rooms">
          {levels.map((l) => {
            const rs = scene.rooms.filter((r) => r.level === l.name);
            if (!rs.length) return null;
            return (
              <div key={l.name}>
                <h4>{l.name === 'Roof' ? 'Roof' : `Level ${l.name}`} <span className="muted">+{l.elevation_m} m</span></h4>
                <ul>
                  {rs.map((r) => (
                    <li key={r.id}>
                      <button type="button" className={hover === r.id ? 'is-current' : undefined}
                              onClick={() => onEnter(r.id)} onMouseEnter={() => onHover(r.id)} onMouseLeave={() => onHover(null)}
                              onFocus={() => onHover(r.id)} onBlur={() => onHover(null)}>
                        <span className="swatch" style={{ background: roomSwatch(r, layer) }} aria-hidden />
                        <span className="name">{r.name}</span>
                        <span className="muted">{r.rack_count ? `${r.rack_count} racks` : r.room_class === 'support' ? 'support' : 'plant'}</span>
                        {layer === 'alarm'
                          ? <span className="count">{r.device_count}</span>
                          : <span className="count" title={roomLayerText(r, layer) ?? undefined}>{roomFigure(r, layer)}</span>}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
        </div>
        {unplaced.length > 0 && (
          <p className="muted bld-note">Not placed: {unplaced.map((r) => r.name).join(', ')} - no position in the building yet.</p>
        )}
      </Section>
    </div>
  );
}

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

/** What a box in a rack says about itself, by what it is.
 *
 *  Each class of gear has its own first screen in a real hall, because each
 *  answers to a different system: a server to its BMC (Redfish: air in and
 *  out, draw, supplies), a switch to IF-MIB (ports, peers, control plane), a
 *  rack strip to the PDU MIB (load, current, what it feeds and what feeds it),
 *  a door probe to the environmental sensor it is (temperature, humidity,
 *  against the ASHRAE envelope), an in-rack CDU to its BMS card (the water
 *  loop). So the panel's head, its tiles and its own section follow the class;
 *  the readings, the conditions and the links are the same for all. Every
 *  figure is one the device page also shows, from the same endpoints. */
type DeviceClass = 'server' | 'network' | 'pdu' | 'sensor' | 'cdu' | 'other';

function deviceClass(t: string): DeviceClass {
  switch (t) {
    case 'server': case 'storage': return 'server';
    case 'switch': case 'oob_switch': case 'router': case 'firewall': case 'load_balancer': return 'network';
    case 'pdu': return 'pdu';
    case 'sensor': return 'sensor';
    case 'cdu': return 'cdu';
    default: return 'other';
  }
}

const num = (m: MetricValue | undefined): number | null => (typeof m?.v === 'number' ? m.v : null);
const fW = (w: number | null | undefined) => (w == null ? '–' : w >= 1000 ? `${(w / 1000).toFixed(2)} kW` : `${Math.round(w)} W`);

export function DevicePanel({ device, rack }: { device: TwinDevice; rack?: FloorRack }) {
  const detail = useQuery<DeviceDetail>({
    queryKey: ['device', device.id],
    queryFn: () => api.device(device.id),
    staleTime: 60_000,
  });
  const state = useQuery<DeviceState>({
    queryKey: ['device-state', device.id],
    queryFn: () => api.deviceState(device.id),
    refetchInterval: 30_000,
    // A device with no telemetry yet is a normal state, not an error.
    retry: false,
  });
  const d = detail.data;
  const m = useMemo(() => state.data?.metrics ?? {}, [state.data]);
  const cls = deviceClass(device.device_type);
  const status = state.data?.status ?? device.status;
  const sev = state.data?.max_severity ?? device.max_severity;
  const seen = state.data?.last_seen ?? d?.last_seen ?? null;
  // The hour behind each headline tile. One request for the class's few
  // keys, refreshed each minute: the tiles say what is, the lines say
  // which way it has been going, and nobody has to leave the viewer to ask.
  const sparkKeys = SPARK_METRICS[cls];
  const spark = useQuery<HistoryOut>({
    queryKey: ['spark', device.id, cls],
    queryFn: () => {
      const end = Date.now();
      return api.history(device.id, sparkKeys, new Date(end - 3600_000).toISOString(), new Date(end).toISOString());
    },
    enabled: sparkKeys.length > 0,
    refetchInterval: 60_000,
    retry: false,
  });
  const sparkBy = useMemo(() => {
    const out: Record<string, number[][]> = {};
    for (const s of spark.data?.series ?? []) if (!out[s.metric] && s.points.length > 1) out[s.metric] = s.points;
    return out;
  }, [spark.data]);
  const openAlarms = state.data?.active_alarms ?? 0;
  const ctx: ClassCtx = { device, rack, d, m, cls, spark: sparkBy, stateLoading: state.isLoading, stateError: state.isError };

  return (
    <div className="vw-panel-body">
      <p className="vw-make">
        {d ? `${d.vendor ?? 'Unknown make'}${d.model ? ` · ${d.model}` : ''}`
           : [device.vendor, device.model].filter(Boolean).join(' · ') || humanise(device.device_type)}
      </p>
      <div className={`vw-state is-${status.toLowerCase()}`}>
        <span className="dot" aria-hidden />
        <b>{status.toLowerCase()}</b>
        {sev !== 'CLEAR' && <span className={`vw-sev sev-${sev.toLowerCase()}`}>{sev.toLowerCase()}</span>}
        <span className="muted">{seen ? `seen ${relativeTime(seen)}` : 'never seen'}</span>
      </div>
      {/* The way out, up top where the hand already is: each goes to the
          matching tab of the device page, so "Alarms" lands on the alarms
          and "Edit" opens the editor rather than a page to find it on. */}
      <div className="vw-acts" role="group" aria-label="Open this device">
        <Link className="vw-icon" to={`/devices/${device.id}`}>Open</Link>
        {atLeast('operator') && <Link className="vw-icon" to={`/devices/${device.id}?edit=1`}>Edit</Link>}
        <Link className={`vw-icon${openAlarms ? ` has-${(sev || 'clear').toLowerCase()}` : ''}`}
              to={`/devices/${device.id}?tab=alarms`}>
          Alarms{openAlarms > 0 && <span className="n">{openAlarms}</span>}
        </Link>
        <Link className="vw-icon" to={`/devices/${device.id}?tab=operations`}>Operations</Link>
      </div>
      {cls === 'server' && <ServerBody {...ctx} />}
      {cls === 'network' && <NetworkBody {...ctx} />}
      {cls === 'pdu' && <PduBody {...ctx} />}
      {cls === 'sensor' && <SensorBody {...ctx} />}
      {cls === 'cdu' && <CduBody {...ctx} />}
      {cls === 'other' && <OtherBody {...ctx} />}
      <UnitAlarms deviceId={device.id} deviceName={device.name} />
      <div className="vw-links">
        <Link to={`/assets/inventory/${device.id}`}>Asset record</Link>
        {rack && <Link to={`/racks/${rack.id}?from=floorplan`}>Rack elevation</Link>}
      </div>
    </div>
  );
}

interface ClassCtx {
  device: TwinDevice; rack?: FloorRack; d?: DeviceDetail; m: Record<string, MetricValue>; cls: DeviceClass;
  /** The last hour of each headline metric, by key. */
  spark: Record<string, number[][]>;
  stateLoading: boolean; stateError: boolean;
}

// ---------------------------------------------------------------- shared pieces

function slotOf(device: TwinDevice): string {
  if (device.u_start) return `U${device.u_start}${device.u_height > 1 ? `–${device.u_start + device.u_height - 1}` : ''}`;
  switch (device.mount) {
    case 'zero_u': return 'zero-U, rear';
    case 'rack_front': return `front door${device.mount_height_m != null ? `, ${device.mount_height_m.toFixed(1)} m up` : ''}`;
    case 'rack_rear': return `rear door${device.mount_height_m != null ? `, ${device.mount_height_m.toFixed(1)} m up` : ''}`;
    default: return humanise(device.mount ?? '') || '–';
  }
}

/** The inventory rows a class wants; `full` adds the commercial ones. */
function Identity({ device, rack, d, full = true, open = true }: ClassCtx & { full?: boolean; open?: boolean }) {
  return (
    <Section title="Identity" open={open}>
      <div className="vw-kv">
        <Row k="Type" v={humanise(device.device_type)} />
        {rack && <Row k="Rack" v={`${rack.name}${rack.row_name ? ` · row ${rack.row_name}` : ''}`} />}
        <Row k="Slot" v={slotOf(device)} />
        <Row k="Serial" v={<span className="mono">{d?.serial_number ?? '–'}</span>} />
        {(full || d?.asset_tag) && <Row k="Asset tag" v={<span className="mono">{d?.asset_tag ?? '–'}</span>} />}
        {(full || d?.mgmt_ip) && <Row k="Management IP" v={<span className="mono">{d?.mgmt_ip ?? '–'}</span>} />}
        {d?.primary_ip && <Row k="Production IP" v={<span className="mono">{d.primary_ip}</span>} />}
        <Row k="Lifecycle" v={d?.lifecycle ? humanise(d.lifecycle) : '–'} />
        {d?.warranty_expires && (
          <Row k="Warranty" v={
            <span className={d.warranty_state === 'expired' ? 'vw-bad' : d.warranty_state === 'expiring' ? 'vw-warn' : undefined}>
              {d.warranty_state === 'expired' ? 'expired ' : 'to '}
              {new Date(d.warranty_expires).toLocaleDateString(undefined, { dateStyle: 'medium' })}
            </span>} />
        )}
        {full && d?.owner_group && <Row k="Owner" v={d.owner_group} />}
      </div>
    </Section>
  );
}

/** Every reading the collector last brought back, in the order the device's
 *  own page charts them, then the rest by the catalogue's group. Per-port
 *  counters are left to the device page: a switch has a hundred of them. */
function Readings({ device, m, stateLoading, stateError, d, skip = [] }: ClassCtx & { skip?: string[] }) {
  const groups = useMemo(() => {
    const keys = Object.keys(m).filter((k) => !k.startsWith('if_') && !skip.includes(k));
    const used = new Set<string>();
    const out: { title: string; keys: string[] }[] = [];
    for (const g of chartGroupsFor(device.device_type) ?? []) {
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
  }, [m, device.device_type, skip]);
  const n = groups.reduce((a, g) => a + g.keys.length, 0);
  const draw = num(m.power_draw) ?? device.power_w ?? null;
  const rated = d?.rated_power_w ?? null;
  // Everything the device reports is already in the class's own tiles and
  // rows: no second list of the same numbers, and no "nothing reported".
  if (!stateLoading && n === 0 && skip.length > 0 && Object.keys(m).some((k) => skip.includes(k)) && rated == null) return null;
  return (
    <Section title={n ? `Readings (${n})` : 'Readings'} open>
      {stateLoading && <p className="muted vw-note">Reading…</p>}
      {!stateLoading && n === 0 && (
        <p className="muted vw-note">
          {stateError ? 'No telemetry yet from this device.'
            : Object.keys(m).some((k) => skip.includes(k)) ? 'Every reading is shown above.' : 'Nothing reported yet.'}
        </p>
      )}
      {groups.map((g) => (
        <div key={g.title} className="vw-readings">
          <p className="vw-sub">{g.title}</p>
          {g.keys.map((k) => <Reading key={k} k={k} m={m[k]} />)}
        </div>
      ))}
      {rated != null && (
        <p className="muted vw-note">
          Rated {rated} W{draw != null ? ` · drawing ${Math.round(draw)} W, ${Math.round((100 * draw) / rated)} % of nameplate` : ''}.
        </p>
      )}
    </Section>
  );
}

/** One reading: the catalogue's name, the value in its unit, how old it is.
 *  A reading the collector flagged (stale, out of range) is said so, not shown
 *  as a confident number. */
function Reading({ k, m }: { k: string; m: MetricValue }) {
  const good = m.q === 'good';
  const unit = METRICS[k as MetricKey]?.unit;
  const val = formatMetric(k, m.v);
  const bare = typeof m.v === 'number' && unit && !/[a-zA-Z°%]/.test(val);
  return (
    <div className={`vw-reading${good ? '' : ' is-flagged'}`} title={good ? undefined : `quality: ${m.q}`}>
      <span className="k">{metricLabel(k)}</span>
      <span className="v">{val}{bare ? ` ${unit}` : ''}</span>
      <span className="t">{m.t ? relativeTime(m.t) : ''}</span>
    </div>
  );
}

/** The cords: each supply, what strip and outlet it lands on, and whether the
 *  pair is redundancy or just two cords. */
function Supplies({ d, open = false }: { d?: DeviceDetail; open?: boolean }) {
  if (!d || d.psus.length === 0) return null;
  return (
    <Section title={`Power supplies (${d.psus.length})`} open={open}>
      <div className="vw-kv">
        {d.psus.map((p) => (
          <Row key={p.number}
               k={`PSU${p.number}${p.rated_watts ? ` · ${p.rated_watts} W` : ''}${p.connector ? ` ${p.connector}` : ''}`}
               v={p.feed_device_id
                 ? <><Link to={`/devices/${p.feed_device_id}`}>{p.feed_device}</Link>
                     {p.feed_outlet != null && <span className="muted"> · outlet {p.feed_outlet}</span>}</>
                 : <span className="vw-warn">not corded</span>} />
        ))}
      </div>
      <p className="muted vw-note"><PsuVerdict psus={d.psus} /></p>
    </Section>
  );
}

/** Two supplies are redundancy only when something separate feeds each. */
function PsuVerdict({ psus }: { psus: DeviceDetail['psus'] }) {
  const feeds = new Set(psus.map((p) => p.feed_device_id).filter((x): x is string => Boolean(x)));
  const uncorded = psus.filter((p) => !p.feed_device_id).length;
  if (uncorded > 0) return <>{uncorded === 1 ? 'One supply' : `${uncorded} supplies`} fitted but not corded.</>;
  if (psus.length === 1) return <>Single-corded: one feed, no redundancy.</>;
  if (feeds.size > 1) return <>Fed from {feeds.size} independent sources.</>;
  return <>Both cords land on one strip: a pair of supplies, one point of failure.</>;
}

function Endpoints({ d }: { d?: DeviceDetail }) {
  if (!d || d.endpoints.length === 0) return null;
  return (
    <Section title={`Endpoints (${d.endpoints.length})`}>
      <div className="vw-kv">
        {d.endpoints.map((e) => (
          <Row key={e.id} k={`${humanise(e.protocol)} · ${humanise(e.role)}`}
               v={<span className={e.status === 'ONLINE' ? undefined : 'vw-warn'}>
                    {(e.status ?? 'unknown').toLowerCase()}
                    {e.last_seen ? <span className="muted"> · {relativeTime(e.last_seen)}</span> : null}
                  </span>} />
        ))}
      </div>
    </Section>
  );
}

/** The port inventory: fitted by speed and role, how many are patched, and
 *  where the patched ones go. A rack with no spare ports is a constraint on
 *  the next install, which a count alone never says. */
function Ports({ deviceId, open, max = 12 }: { deviceId: string; open: boolean; max?: number }) {
  const q = useQuery<NetworkInterface[]>({
    queryKey: ['device-interfaces', deviceId],
    queryFn: () => api.interfaces(deviceId),
    retry: false,
    staleTime: 300_000,
  });
  const ports = q.data ?? [];
  const patched = ports.filter((p) => p.peer_device_id);
  const buckets = new Map<string, number>();
  for (const p of ports) {
    const k = `${formatSpeed(p.speed_bps)} ${humanise(p.role)}`;
    buckets.set(k, (buckets.get(k) ?? 0) + 1);
  }
  // Where the cables go, by far-end device: the uplinks and the servers behind
  // a leaf, the leaves behind a spine, without listing every port.
  const peers = new Map<string, { id: string; n: number; layer: string | null }>();
  for (const p of patched) {
    const e = peers.get(p.peer_device!) ?? { id: p.peer_device_id!, n: 0, layer: p.peer_layer ?? null };
    e.n += 1; peers.set(p.peer_device!, e);
  }
  const peerRows = [...peers.entries()].sort((a, b) => b[1].n - a[1].n);
  const title = q.isLoading ? 'Ports' : `Ports (${patched.length} of ${ports.length} patched)`;
  return (
    <Section title={title} open={open}>
      {q.isError && <p className="muted vw-note">The port list could not be read.</p>}
      {q.data && ports.length === 0 && <p className="muted vw-note">No ports recorded.</p>}
      {ports.length > 0 && (
        <>
          <p className="vw-note">
            {[...buckets.entries()].map(([k, n]) => `${n} × ${k}`).join(', ')}
            {ports.length - patched.length > 0 ? ` · ${ports.length - patched.length} spare` : ' · none spare'}
          </p>
          <div className="vw-kv">
            {peerRows.slice(0, max).map(([name, e]) => (
              <Row key={name} k={<><Link to={`/devices/${e.id}`}>{name}</Link>{e.layer && <span className="muted"> · {e.layer}</span>}</>}
                   v={`${e.n} ${e.n === 1 ? 'cable' : 'cables'}`} />
            ))}
          </div>
          {peerRows.length > max && <p className="muted vw-note">and {peerRows.length - max} more far ends on the device page.</p>}
        </>
      )}
    </Section>
  );
}

// ---------------------------------------------------------------- classes

/** A server: the air it breathes and blows, what it draws, its cords. */
function ServerBody(c: ClassCtx) {
  const { device, m } = c;
  const inlet = num(m.inlet_temperature) ?? device.temp_c ?? null;
  const exhaust = num(m.exhaust_temperature) ?? device.exhaust_c ?? null;
  const draw = num(m.power_draw) ?? device.power_w ?? null;
  const cpu = num(m.cpu_temperature);
  return (
    <>
      <Tiles spark={c.spark} items={[
        { label: 'POWER', value: fW(draw), metric: 'power_draw' },
        { label: 'INLET', value: fmtT(inlet), metric: 'inlet_temperature' },
        { label: cpu != null ? 'CPU' : 'EXHAUST', value: cpu != null ? fmtT(cpu) : fmtT(exhaust),
          metric: cpu != null ? 'cpu_temperature' : 'exhaust_temperature' },
      ]} />
      {exhaust != null && inlet != null && (
        <div className="vw-kv">
          <Row k="Exhaust" v={fmtT(exhaust)} />
          <Row k="Rise in → out" v={fmtDT(exhaust - inlet)} />
        </div>
      )}
      <Identity {...c} />
      <Readings {...c} />
      <Supplies d={c.d} open />
      <Ports deviceId={device.id} open={false} max={6} />
      <Endpoints d={c.d} />
    </>
  );
}

/** A switch, router, firewall or balancer: its ports and their far ends
 *  first, the control plane under them. */
function NetworkBody(c: ClassCtx) {
  const { device, m } = c;
  const cpu = num(m.cpu_utilization), mem = num(m.memory_utilization), t = num(m.cpu_temperature);
  return (
    <>
      <Tiles spark={c.spark} items={[
        { label: 'CPU', value: cpu != null ? `${Math.round(cpu)} %` : '–', metric: 'cpu_utilization' },
        { label: 'MEMORY', value: mem != null ? `${Math.round(mem)} %` : '–', metric: 'memory_utilization' },
        { label: 'CPU TEMP', value: fmtT(t), metric: 'cpu_temperature' },
      ]} />
      <Ports deviceId={device.id} open />
      <Identity {...c} />
      <Readings {...c} />
      <Supplies d={c.d} />
      <Endpoints d={c.d} />
    </>
  );
}

/** A rack strip: how full it is, what feeds it, and what hangs off it. */
function PduBody(c: ClassCtx) {
  const { device, m, d } = c;
  const chain = useQuery<PowerChain>({
    queryKey: ['power-chain', device.id],
    queryFn: () => api.powerChain(device.id),
    retry: false, staleTime: 60_000,
  });
  const impact = useQuery<Impact>({
    queryKey: ['impact', device.id],
    queryFn: () => api.impact(device.id),
    retry: false, staleTime: 60_000,
  });
  const load = num(m.load_pct), amps = num(m.current), draw = num(m.power_draw) ?? device.power_w ?? null;
  const volts = num(m.voltage_ln) ?? num(m.voltage_ll);
  const rated = d?.rated_power_w ?? null;
  const tone = load == null ? undefined : load >= 90 ? 'vw-bad' : load >= 80 ? 'vw-warn' : undefined;
  const power = impact.data?.layers.find((l) => l.layer === 'power');
  const loads = power ? [...power.cut_off, ...power.degraded] : [];
  const side = chain.data?.paths[0]?.side ?? null;
  return (
    <>
      <Tiles spark={c.spark} items={[
        { label: 'LOAD', value: load != null ? `${load.toFixed(0)} %` : '–', metric: 'load_pct' },
        { label: 'CURRENT', value: amps != null ? `${amps.toFixed(1)} A` : '–', metric: 'current' },
        { label: 'POWER', value: fW(draw), metric: 'power_draw' },
      ]} />
      <div className="vw-kv">
        {rated != null && <Row k="Rated" v={<span className={tone}>{fW(rated)}{draw != null ? ` · ${Math.round((100 * draw) / rated)} % used` : ''}</span>} />}
        {volts != null && <Row k="Input voltage" v={`${Math.round(volts)} V`} />}
        {num(m.energy_consumed) != null && <Row k="Energy" v={formatMetric('energy_consumed', m.energy_consumed.v)} />}
        {(num(m.ambient_temperature) != null || device.temp_c != null) && (
          <Row k="Air at the strip" v={`${fmtT(num(m.ambient_temperature) ?? device.temp_c)}${num(m.relative_humidity) != null ? ` · ${Math.round(num(m.relative_humidity)!)} % RH` : ''}`} />
        )}
      </div>
      <Section title={`Fed from${side ? ` · side ${side}` : ''}`} open>
        {chain.isLoading && <p className="muted vw-note">Tracing the feed…</p>}
        {chain.isError && <p className="muted vw-note">No power path recorded for this strip.</p>}
        {chain.data && (
          <>
            <ol className="vw-chain">
              {chain.data.paths[0]?.hops.map((h) => (
                <li key={h.device_id}>
                  <Link to={`/devices/${h.device_id}`}>{h.name}</Link>
                  <span className="muted"> · {humanise(h.device_type)}{h.load_pct != null ? ` · ${Math.round(h.load_pct)} %` : h.load_w != null ? ` · ${fW(h.load_w)}` : ''}</span>
                  {h.status === 'OFFLINE' && <span className="vw-bad"> · offline</span>}
                </li>
              ))}
            </ol>
            <p className="muted vw-note">{humanise(chain.data.redundancy)} · {chain.data.reason}.</p>
          </>
        )}
      </Section>
      <Section title={power ? `Feeds (${power.dependents})` : 'Feeds'} open>
        {impact.isLoading && <p className="muted vw-note">Counting the loads…</p>}
        {impact.isError && <p className="muted vw-note">The load list could not be read.</p>}
        {power && loads.length === 0 && <p className="muted vw-note">Nothing corded to this strip.</p>}
        {power && loads.length > 0 && (
          <>
            {power.cut_off.length > 0 && (
              <p className="vw-note vw-warn">{power.cut_off.length} {power.cut_off.length === 1 ? 'load goes' : 'loads go'} dark if this strip is lost; the rest keep their other cord.</p>
            )}
            <ul className="vw-list vw-loads">
              {loads.slice(0, 14).map((x) => (
                <li key={x.id}><Link to={`/devices/${x.id}`}>{x.name}</Link>
                  <span className="muted"> · {humanise(x.device_type)}{power.cut_off.some((y) => y.id === x.id) ? ' · only cord' : ''}</span></li>
              ))}
            </ul>
            {loads.length > 14 && <p className="muted vw-note">and {loads.length - 14} more on the device page.</p>}
          </>
        )}
      </Section>
      <Identity {...c} full={false} open={false} />
      <Readings {...c} skip={['load_pct', 'current', 'power_draw', 'ambient_temperature', 'relative_humidity', 'voltage_ln', 'voltage_ll', 'energy_consumed']} />
      <Endpoints d={c.d} />
    </>
  );
}

/** ASHRAE TC 9.9 class A1: the recommended intake envelope and the allowable one. */
const A1 = { recLo: 18, recHi: 27, allLo: 15, allHi: 32, rhHi: 60, dewHi: 15 };

function envelopeVerdict(t: number | null, rh: number | null): { text: string; tone?: string } | null {
  if (t == null) return null;
  if (t < A1.allLo || t > A1.allHi) return { text: `outside the A1 allowable band (${A1.allLo}–${A1.allHi} °C)`, tone: 'vw-bad' };
  if (t < A1.recLo || t > A1.recHi) return { text: `outside the A1 recommended band (${A1.recLo}–${A1.recHi} °C), inside allowable`, tone: 'vw-warn' };
  if (rh != null && rh > A1.rhHi) return { text: `temperature in band; humidity over ${A1.rhHi} % RH`, tone: 'vw-warn' };
  const dp = dewPoint(t, rh);
  if (dp != null && dp > A1.dewHi) return { text: `temperature in band; dew point over ${A1.dewHi} °C`, tone: 'vw-warn' };
  return { text: 'inside the A1 recommended envelope' };
}

/** A door probe: what the air is where it hangs, against the envelope. */
function SensorBody(c: ClassCtx) {
  const { device, m } = c;
  const t = num(m.ambient_temperature) ?? device.temp_c ?? null;
  const rh = num(m.relative_humidity) ?? device.rh_pct ?? null;
  const dp = dewPoint(t, rh);
  const v = envelopeVerdict(t, rh);
  const where = device.mount === 'rack_rear' ? 'exhaust side' : device.mount === 'rack_front' ? 'intake side' : null;
  return (
    <>
      <Tiles spark={c.spark} items={[
        { label: 'TEMP', value: fmtT(t), metric: 'ambient_temperature' },
        { label: 'RH', value: f1(rh, ' %'), metric: 'relative_humidity' },
        { label: 'DEW POINT', value: fmtT(dp) },
      ]} />
      <div className="vw-kv">
        <Row k="Hangs on" v={`${slotOf(device)}${where ? ` · ${where}` : ''}`} />
        {v && device.mount !== 'rack_rear' && <Row k="Envelope" v={<span className={v.tone}>{v.text}</span>} />}
        {device.mount === 'rack_rear' && t != null && <Row k="Exhaust" v="not graded: the envelope is for intake air" />}
      </div>
      <Readings {...c} skip={['ambient_temperature', 'relative_humidity', 'dew_point']} />
      <Identity {...c} full={false} />
      <Endpoints d={c.d} />
    </>
  );
}

/** An in-rack coolant distribution unit: the loop it serves, its state, its cords. */
function CduBody(c: ClassCtx) {
  const { device, m } = c;
  const supply = num(m.water_supply_temp), ret = num(m.water_return_temp), flow = num(m.water_flow);
  const draw = num(m.power_draw) ?? device.power_w ?? null;
  const running = typeof m.equipment_state?.v === 'boolean' ? m.equipment_state.v : null;
  const alarm = typeof m.alarm_state?.v === 'boolean' ? m.alarm_state.v : null;
  return (
    <>
      <Tiles spark={c.spark} items={[
        { label: 'SUPPLY', value: fmtT(supply), metric: 'water_supply_temp' },
        { label: flow != null && ret == null ? 'FLOW' : 'RETURN', value: ret != null ? fmtT(ret) : flow != null ? formatMetric('water_flow', flow) : '–',
          metric: flow != null && ret == null ? 'water_flow' : 'water_return_temp' },
        { label: 'POWER', value: fW(draw), metric: 'power_draw' },
      ]} />
      <div className="vw-kv">
        {running != null && <Row k="Pumps" v={running ? 'running' : <span className="vw-bad">stopped</span>} />}
        {alarm != null && <Row k="Unit alarm" v={alarm ? <span className="vw-bad">raised</span> : 'none'} />}
        {supply != null && ret != null && <Row k="Loop ΔT" v={fmtDT(ret - supply)} />}
      </div>
      <Readings {...c} skip={['equipment_state', 'alarm_state', 'water_supply_temp', 'water_return_temp', 'water_flow', 'power_draw']} />
      <Supplies d={c.d} open />
      <Identity {...c} open={false} />
      <Endpoints d={c.d} />
    </>
  );
}

function OtherBody(c: ClassCtx) {
  const { device, m } = c;
  const draw = num(m.power_draw) ?? device.power_w ?? null;
  return (
    <>
      <Tiles spark={c.spark} items={[
        { label: 'AIR', value: fmtT(device.temp_c) },
        { label: 'RH', value: f1(device.rh_pct, ' %') },
        { label: 'POWER', value: fW(draw), metric: 'power_draw' },
      ]} />
      <Identity {...c} />
      <Readings {...c} />
      <Supplies d={c.d} />
      <Endpoints d={c.d} />
    </>
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

/** The building and site rungs' layers: what colours a room, and which
 *  level is in focus. Radios, like the room's filters, so the dock's two
 *  Layers doors read the same. */
export function LayerPanel({ layer, onLayer, levels, level, onLevel }: {
  layer: BuildingLayer; onLayer: (l: BuildingLayer) => void;
  /** Level names in order; empty at the site rung, where there is no level to focus. */
  levels: string[]; level: string | null; onLevel: (l: string | null) => void;
}) {
  return (
    <div className="vw-panel-body">
      <details className="vw-section" open>
        <summary>Rooms</summary>
        <div className="vw-section-body vw-radios" role="radiogroup" aria-label="Room layer">
          {BUILDING_LAYERS.map((l) => (
            <label key={l.key} className={l.key === layer ? 'is-on' : undefined}>
              <input type="radio" name="room-layer" checked={l.key === layer} onChange={() => onLayer(l.key)} />
              <span>{l.label}</span>{l.hint && <small>{l.hint}</small>}
            </label>
          ))}
        </div>
      </details>
      {levels.length > 1 && (
        <details className="vw-section" open>
          <summary>Level</summary>
          <div className="vw-section-body vw-radios" role="radiogroup" aria-label="Level in focus">
            <label className={level == null ? 'is-on' : undefined}>
              <input type="radio" name="level" checked={level == null} onChange={() => onLevel(null)} />
              <span>All levels</span>
            </label>
            {levels.map((n) => (
              <label key={n} className={n === level ? 'is-on' : undefined}>
                <input type="radio" name="level" checked={n === level} onChange={() => onLevel(n)} />
                <span>{/^\d+$/.test(n) ? `Level ${n}` : n}</span>
              </label>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

export function FiltersPanel({ rackLayer, coolingLayer, heat, vis, onRack, onCooling, onHeat, onVis }: {
  rackLayer: RackLayer; coolingLayer: CoolingLayer; heat: HeatPlane; vis: Visibility;
  onRack: (l: RackLayer) => void; onCooling: (l: CoolingLayer) => void; onHeat: (h: HeatPlane) => void;
  onVis: (v: Visibility) => void;
}) {
  const [open, setOpen] = useState<'racks' | 'cooling' | 'heat' | 'vis'>('racks');
  const VIS: { key: keyof Visibility; label: string }[] = [
    { key: 'doorSensors', label: 'Rack door sensors' }, { key: 'plant', label: 'Plant and panels' },
    { key: 'aisles', label: 'Aisle tint' }, { key: 'containment', label: 'Containment' },
    { key: 'vents', label: 'Vent tiles' }, { key: 'hotspots', label: 'Hot-spot markers' },
    { key: 'faces', label: 'See into racks' },
    { key: 'underfloor', label: 'See under the raised floor' },
    { key: 'fittings', label: 'Doors and room fittings' },
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
