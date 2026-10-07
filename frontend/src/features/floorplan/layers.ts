import type { FloorEquipment, FloorRack, ThermalUnit, TwinDevice } from '../../api/client';
import { resolveColor } from './colors';

/**
 * What the room viewer can colour racks and cooling units by, and the discrete
 * bands each layer is read on. One registry for the 3D scene, the PLAN view and
 * the legend, so a colour in the picture always means what the legend says.
 *
 * Bands rather than a continuous ramp: an operator reads "that rack is in the
 * 27-30 band" off the legend, which a gradient cannot give them. The
 * temperature bands are the ASHRAE A1 envelope cut finer: 18-27 recommended,
 * 15-32 allowable, so green IS recommended and red IS out of the allowable.
 */

export const U = 0.04445;          // one rack unit (EIA-310)
export const RACK_BASE = 0.1;      // U1's bottom edge above the floor
export const TILE = 0.6;           // raised-floor tile pitch

export type RackLayer = 'temperature' | 'rh' | 'power' | 'utilisation' | 'space'
  | 'compliance' | 'alarm' | 'none';
export type CoolingLayer = 'air' | 'supply' | 'return' | 'utilisation' | 'none';

export const RACK_LAYERS: { key: RackLayer; label: string; hint: string }[] = [
  { key: 'temperature', label: 'Temperature', hint: 'Inlet air, bottom to top of the rack' },
  { key: 'compliance', label: 'ASHRAE compliance', hint: 'Against the A1 envelope' },
  { key: 'rh', label: 'Relative humidity', hint: 'Probes on the rack' },
  { key: 'power', label: 'Power usage', hint: 'kW metered at the rack PDUs' },
  { key: 'utilisation', label: 'Power utilisation', hint: 'Load against the rack rating' },
  { key: 'space', label: 'Space', hint: 'U in use' },
  { key: 'alarm', label: 'Alarms', hint: 'Worst open condition in the rack' },
  { key: 'none', label: 'None', hint: 'Plain cabinets' },
];

export const COOLING_LAYERS: { key: CoolingLayer; label: string; hint: string }[] = [
  { key: 'air', label: 'Supply → return', hint: 'Discharge at the base, return at the top' },
  { key: 'supply', label: 'Supply temperature', hint: '' },
  { key: 'return', label: 'Return temperature', hint: '' },
  { key: 'utilisation', label: 'Cooling utilisation', hint: 'Delivered against rating' },
  { key: 'none', label: 'None', hint: '' },
];

export interface Band { upTo: number | null; label: string; color: string }
export interface Legend { title: string; unit: string; bands: Band[]; none?: string }

// Palettes are theme tokens (index.css --band-*), resolved at render; the
// viewer never carries a hex of its own.
const T = (n: number) => `var(--band-t${n})`;
const P = (n: number) => `var(--band-p${n})`;

export const TEMP_BANDS: Band[] = [
  { upTo: 15, label: '< 15', color: T(1) },
  { upTo: 18, label: '15 – 18', color: T(2) },
  { upTo: 21, label: '18 – 21', color: T(3) },
  { upTo: 24, label: '21 – 24', color: T(4) },
  { upTo: 27, label: '24 – 27', color: T(5) },
  { upTo: 30, label: '27 – 30', color: T(6) },
  { upTo: 32, label: '30 – 32', color: T(7) },
  { upTo: null, label: '> 32', color: T(8) },
];

export const RH_BANDS: Band[] = [
  { upTo: 20, label: '< 20', color: T(8) },
  { upTo: 30, label: '20 – 30', color: T(7) },
  { upTo: 40, label: '30 – 40', color: T(6) },
  { upTo: 60, label: '40 – 60', color: T(4) },
  { upTo: 70, label: '60 – 70', color: T(6) },
  { upTo: 80, label: '70 – 80', color: T(7) },
  { upTo: null, label: '> 80', color: T(8) },
];

export const PCT_BANDS: Band[] = [
  { upTo: 20, label: '< 20', color: P(1) },
  { upTo: 50, label: '20 – 50', color: P(2) },
  { upTo: 80, label: '50 – 80', color: P(3) },
  { upTo: 90, label: '80 – 90', color: P(4) },
  { upTo: null, label: '> 90', color: P(5) },
];

export const KW_BANDS: Band[] = [
  { upTo: 2, label: '< 2', color: T(3) },
  { upTo: 5, label: '2 – 5', color: T(4) },
  { upTo: 10, label: '5 – 10', color: T(5) },
  { upTo: 15, label: '10 – 15', color: T(6) },
  { upTo: 20, label: '15 – 20', color: T(7) },
  { upTo: null, label: '> 20', color: T(8) },
];

export const SPACE_BANDS: Band[] = [
  { upTo: 20, label: '< 20', color: P(1) },
  { upTo: 50, label: '20 – 50', color: P(2) },
  { upTo: 80, label: '50 – 80', color: P(3) },
  { upTo: 95, label: '80 – 95', color: P(4) },
  { upTo: null, label: 'Full', color: P(5) },
];

export const COMPLIANCE_BANDS: Band[] = [
  { upTo: 0, label: 'Below recommended', color: T(2) },
  { upTo: 1, label: 'Recommended', color: T(4) },
  { upTo: 2, label: 'Above recommended', color: T(6) },
  { upTo: null, label: 'Out of allowable', color: T(8) },
];

export const ALARM_BANDS: Band[] = [
  { upTo: 0, label: 'Clear', color: 'var(--ok)' },
  { upTo: 1, label: 'Minor / warning', color: 'var(--warn)' },
  { upTo: 2, label: 'Major', color: 'var(--major)' },
  { upTo: 3, label: 'Critical', color: 'var(--critical)' },
  { upTo: null, label: 'Offline', color: 'var(--band-t1)' },
];

export const NO_READING = 'var(--band-none)';

export function bandColor(bands: Band[], v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return NO_READING;
  for (const b of bands) if (b.upTo == null || v < b.upTo) return b.color;
  return bands[bands.length - 1].color;
}

export function legendFor(layer: RackLayer): Legend | null {
  switch (layer) {
    case 'temperature': return { title: 'Temperature', unit: '°C', bands: TEMP_BANDS, none: 'no reading' };
    case 'rh': return { title: 'Relative humidity', unit: '%', bands: RH_BANDS, none: 'no probe' };
    case 'power': return { title: 'Power usage', unit: 'kW', bands: KW_BANDS, none: 'not metered' };
    case 'utilisation': return { title: 'Power utilisation', unit: '%', bands: PCT_BANDS, none: 'no rating' };
    case 'space': return { title: 'Space used', unit: '%', bands: SPACE_BANDS };
    case 'compliance': return { title: 'ASHRAE A1', unit: '', bands: COMPLIANCE_BANDS, none: 'no reading' };
    case 'alarm': return { title: 'Alarms', unit: '', bands: ALARM_BANDS };
    default: return null;
  }
}

export function coolingLegendFor(layer: CoolingLayer): Legend | null {
  switch (layer) {
    case 'air': return { title: 'Cooling unit air', unit: '°C', bands: TEMP_BANDS, none: 'no reading' };
    case 'supply': return { title: 'Supply temperature', unit: '°C', bands: TEMP_BANDS, none: 'no reading' };
    case 'return': return { title: 'Return temperature', unit: '°C', bands: TEMP_BANDS, none: 'no reading' };
    case 'utilisation': return { title: 'Cooling utilisation', unit: '%', bands: PCT_BANDS, none: 'no rating' };
    default: return null;
  }
}

// ---------------------------------------------------------------- readings

/** Mean air temperature in the bottom, middle and top third of a rack, from
 *  every reading in it - server inlets and door probes alike. A third with no
 *  reading takes its nearest neighbour's, so a rack with one probe is one
 *  colour rather than two-thirds grey; a rack with none is null throughout. */
export function rackTiers(devices: TwinDevice[], uHeight: number): [number | null, number | null, number | null] {
  const sums = [0, 0, 0], ns = [0, 0, 0];
  const third = Math.max(1, uHeight / 3);
  for (const d of devices) {
    if (d.temp_c == null) continue;
    let u: number | null = null;
    if (d.u_start != null && d.u_start > 0) u = d.u_start + d.u_height / 2;
    else if (d.mount_height_m != null) u = (d.mount_height_m - RACK_BASE) / U;
    if (u == null) continue;
    const i = Math.min(2, Math.max(0, Math.floor((u - 1) / third)));
    sums[i] += d.temp_c;
    ns[i] += 1;
  }
  const t = sums.map((s, i) => (ns[i] ? s / ns[i] : null));
  const fill = (i: number): number | null => {
    if (t[i] != null) return t[i];
    const order = i === 0 ? [1, 2] : i === 2 ? [1, 0] : [0, 2];
    for (const j of order) if (t[j] != null) return t[j];
    return null;
  };
  return [fill(0), fill(1), fill(2)];
}

export function rackMeanRh(devices: TwinDevice[]): number | null {
  const v = devices.map((d) => d.rh_pct).filter((x): x is number => x != null);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}

/** Magnus formula (Alduchov & Eskridge coefficients), good to 0.1 K indoors. */
export function dewPoint(tC: number | null, rh: number | null): number | null {
  if (tC == null || rh == null || rh <= 0) return null;
  const a = 17.625, b = 243.04;
  const g = Math.log(rh / 100) + (a * tC) / (b + tC);
  return (b * g) / (a - g);
}

export function compliance(tC: number | null): number | null {
  if (tC == null) return null;
  if (tC < 18) return 0;
  if (tC <= 27) return 1;
  if (tC <= 32) return 2;
  return 3;
}

export function alarmRank(sev: string, offline: number): number {
  if (offline > 0) return 4;
  switch (sev) {
    case 'CRITICAL': return 3;
    case 'MAJOR': return 2;
    case 'MINOR':
    case 'WARNING': return 1;
    default: return 0;
  }
}

/** Grid reference in the convention floor plans use: a letter per 600 mm tile
 *  along the row, a number per tile across. */
export function gridRef(x: number, y: number): string {
  return `${colLetters(Math.floor(x / TILE))}${Math.floor(y / TILE) + 1}`;
}

export function colLetters(i: number): string {
  let s = '';
  let n = i;
  do {
    s = String.fromCharCode(65 + (n % 26)) + s;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return s;
}

// ---------------------------------------------------------------- colouring

export interface RackPaint {
  /** Bottom, middle, top colours (resolved CSS colours). */
  bot: string; mid: string; top: string;
  /** Share of the rack's height drawn solid; the rest is a translucent shell.
   *  1 for every layer but the capacity ones. */
  fill: number;
}

export function paintRack(r: FloorRack, devices: TwinDevice[], layer: RackLayer, peakKw: number): RackPaint {
  const uH = r.u_height ?? 42;
  const flat = (c: string, fill = 1): RackPaint => {
    const v = resolveColor(c);
    return { bot: v, mid: v, top: v, fill };
  };
  switch (layer) {
    case 'temperature': {
      const [b, m, t] = rackTiers(devices, uH);
      return { bot: resolveColor(bandColor(TEMP_BANDS, b)), mid: resolveColor(bandColor(TEMP_BANDS, m)),
               top: resolveColor(bandColor(TEMP_BANDS, t)), fill: 1 };
    }
    case 'compliance': {
      const [b, m, t] = rackTiers(devices, uH);
      const worst = [b, m, t].filter((x): x is number => x != null);
      return flat(bandColor(COMPLIANCE_BANDS, worst.length ? compliance(Math.max(...worst)) : null));
    }
    case 'rh': return flat(bandColor(RH_BANDS, rackMeanRh(devices)));
    case 'power': {
      const kw = r.load_kw;
      if (kw == null) return flat(NO_READING);
      const ref = r.rated_power_kw && r.rated_power_kw > 0 ? r.rated_power_kw : peakKw;
      return flat(bandColor(KW_BANDS, kw), ref > 0 ? Math.min(1, Math.max(0.04, kw / ref)) : 1);
    }
    case 'utilisation': {
      if (r.load_kw == null || !r.rated_power_kw) return flat(NO_READING);
      const pct = (100 * r.load_kw) / r.rated_power_kw;
      return flat(bandColor(PCT_BANDS, pct), Math.min(1, Math.max(0.04, pct / 100)));
    }
    case 'space': {
      if (r.free_u == null) return flat(NO_READING);
      const used = 100 * (1 - r.free_u / uH);
      return flat(bandColor(SPACE_BANDS, used), Math.min(1, Math.max(0.04, used / 100)));
    }
    case 'alarm': return flat(bandColor(ALARM_BANDS, alarmRank(r.max_severity, r.offline_count)));
    default: return flat('var(--band-plain)');
  }
}

export interface UnitReadings { supply_c: number | null; return_c: number | null; duty_pct: number | null }

export function unitReadings(e: FloorEquipment, t: ThermalUnit | undefined): UnitReadings {
  return {
    supply_c: t?.supply_c ?? e.supply_c ?? null,
    return_c: t?.return_c ?? e.return_c ?? null,
    duty_pct: t?.duty_pct ?? null,
  };
}

const AIR_HANDLERS = new Set(['crah', 'crac']);
const COOLING = new Set(['crah', 'crac', 'chiller', 'pump', 'cooling_tower', 'cdu', 'valve']);

export function paintEquipment(e: FloorEquipment, layer: CoolingLayer, reads: UnitReadings): RackPaint {
  const flat = (c: string): RackPaint => {
    const v = resolveColor(c);
    return { bot: v, mid: v, top: v, fill: 1 };
  };
  if (e.max_severity !== 'CLEAR') return flat(bandColor(ALARM_BANDS, alarmRank(e.max_severity, 0)));
  if (!AIR_HANDLERS.has(e.device_type) || layer === 'none') {
    return flat(COOLING.has(e.device_type) ? 'var(--band-cooling)' : 'var(--band-power)');
  }
  switch (layer) {
    case 'air': {
      const s = resolveColor(bandColor(TEMP_BANDS, reads.supply_c));
      const r = resolveColor(bandColor(TEMP_BANDS, reads.return_c));
      return { bot: s, mid: s, top: r, fill: 1 };
    }
    case 'supply': return flat(bandColor(TEMP_BANDS, reads.supply_c));
    case 'return': return flat(bandColor(TEMP_BANDS, reads.return_c));
    case 'utilisation': return flat(bandColor(PCT_BANDS, reads.duty_pct));
    default: return flat('var(--band-cooling)');
  }
}

// ---------------------------------------------------------------- viewer state

export type ViewMode = '3d' | 'plan' | 'fpv';
export type Sel = { kind: 'rack' | 'device' | 'equipment'; id: string } | null;

export interface Visibility {
  devices: boolean; plant: boolean; aisles: boolean; containment: boolean;
  labels: boolean; vents: boolean;
}
export const DEFAULT_VIS: Visibility = {
  devices: true, plant: true, aisles: true, containment: true, labels: false, vents: true,
};

/** Perforated tiles: one in front of each rack that faces a cold aisle. The
 *  import carries no vent positions, so this is the layout a contained cold
 *  aisle is normally built with, and the viewer says it is assumed. */
export function ventTiles(racks: FloorRack[], aisles: { y_start: number; y_end: number; kind: string }[],
                          whiteSpace: boolean): { x: number; y: number }[] {
  if (!whiteSpace) return [];
  const out: { x: number; y: number }[] = [];
  for (const r of racks) {
    if (r.facing !== 'N' && r.facing !== 'S') continue;
    const d = r.d_m ?? 1.2;
    const y = r.facing === 'N' ? r.y - d / 2 - TILE / 2 : r.y + d / 2 + TILE / 2;
    const cold = aisles.some((a) => a.kind === 'cold' && y >= a.y_start - 1e-6 && y <= a.y_end + 1e-6);
    if (cold) out.push({ x: r.x, y });
  }
  return out;
}
