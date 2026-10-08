import { useSyncExternalStore } from 'react';
import type { Band, Legend } from './layers';

/**
 * Temperature display unit for the viewer. Data stays in °C end to end - the
 * bands, the indices, the API; only what is printed changes. A per-viewer
 * convenience, so it lives in this browser and nowhere else.
 *
 * Differences convert by the factor alone (1 K = 1.8 °F of difference), and
 * are printed as K in Celsius mode and °F in Fahrenheit mode - the way a US
 * BMS writes a 20 °F coil ΔT.
 */
export type TempUnit = 'C' | 'F';
const KEY = 'dcim.viewer.tempUnit';

function read(): TempUnit {
  try { return localStorage.getItem(KEY) === 'F' ? 'F' : 'C'; } catch { return 'C'; }
}
let current: TempUnit = read();
const subs = new Set<() => void>();

export function setTempUnit(u: TempUnit): void {
  current = u;
  try { localStorage.setItem(KEY, u); } catch { /* private window: this session only */ }
  subs.forEach((f) => f());
}
export function tempUnit(): TempUnit { return current; }
export function useTempUnit(): TempUnit {
  return useSyncExternalStore((f) => { subs.add(f); return () => subs.delete(f); }, () => current, () => 'C');
}

export const toUnit = (c: number): number => (current === 'F' ? c * 1.8 + 32 : c);
export const unitLabel = (): string => (current === 'F' ? '°F' : '°C');
export const deltaLabel = (): string => (current === 'F' ? '°F' : 'K');

/** "23.4 °C" / "74.1 °F"; '–' for no reading. */
export function fmtT(c: number | null | undefined, digits = 1): string {
  return c == null ? '–' : `${toUnit(c).toFixed(digits)} ${unitLabel()}`;
}
/** A bare number in the display unit, for "a / b / c °C" runs. */
export function numT(c: number | null | undefined, digits = 1): string {
  return c == null ? '–' : toUnit(c).toFixed(digits);
}
/** A difference: "11.5 K" / "20.7 °F". */
export function fmtDT(k: number | null | undefined, digits = 1): string {
  return k == null ? '–' : `${(current === 'F' ? k * 1.8 : k).toFixed(digits)} ${deltaLabel()}`;
}

/** A °C (or K) legend, relabelled in the display unit from its band edges. */
export function legendInUnit(spec: Legend): Legend {
  if (current === 'C' || (spec.unit !== '°C' && spec.unit !== 'K')) return spec;
  const conv = spec.unit === '°C' ? (v: number) => v * 1.8 + 32 : (v: number) => v * 1.8;
  const r = (v: number) => String(Math.round(conv(v)));
  const bands: Band[] = spec.bands.map((b, i) => {
    const lo = i > 0 ? spec.bands[i - 1].upTo : null;
    const label = lo == null && b.upTo != null ? `< ${r(b.upTo)}`
      : b.upTo == null && lo != null ? `> ${r(lo)}`
      : lo != null && b.upTo != null ? `${r(lo)} – ${r(b.upTo)}` : b.label;
    return { ...b, label };
  });
  return { ...spec, unit: spec.unit === '°C' ? '°F' : '°F Δ', bands };
}
