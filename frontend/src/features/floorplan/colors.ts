import type { FloorRack } from '../../api/client';

/** One overlay vocabulary for the 2D plan and the 3D room, so the two views of
 *  the same room can never disagree about what a colour means. */
export type Overlay = 'alarm' | 'thermal' | 'power' | 'space';

export const OVERLAYS: { key: Overlay; label: string }[] = [
  { key: 'alarm', label: 'Alarms' },
  { key: 'thermal', label: 'Inlet temp' },
  { key: 'power', label: 'Power' },
  { key: 'space', label: 'Space' },
];

// ASHRAE A1: 18-27 C recommended intake, allowable to 32. The scale is fixed to
// that band rather than to the room's own spread, so a room sitting at a
// uniform 24 C looks uniformly fine instead of manufacturing a hot spot out of
// half a degree.
export const TEMP_MIN = 18;
export const TEMP_RECOMMENDED_MAX = 27;
export const TEMP_MAX = 32;

/** Power bands, as a share of the rack's rating (its smallest single feed). */
export const POWER_WARN = 0.8;
export const POWER_CRIT = 0.9;

export function tempColor(c: number | null | undefined): string {
  if (c == null) return 'var(--bg-inset)';
  const t = Math.min(1, Math.max(0, (c - TEMP_MIN) / (TEMP_MAX - TEMP_MIN)));
  // Blue (cold) through amber to red. hsl hue 210 -> 0.
  return `hsl(${Math.round(210 - 210 * t)}, 70%, ${Math.round(55 - 15 * t)}%)`;
}

/** Against the rating when the rack has one: past 80 % of a single feed is
 *  where a 2N rack stops surviving the loss of the other. Without a rating the
 *  rack can only be compared with its neighbours, and the ramp says so by
 *  never turning amber or red. */
export function powerColor(r: Pick<FloorRack, 'load_kw' | 'rated_power_kw'>, peakKw: number): string {
  const kw = r.load_kw;
  if (kw == null) return 'var(--bg-inset)';
  if (r.rated_power_kw && r.rated_power_kw > 0) {
    const pct = kw / r.rated_power_kw;
    if (pct >= POWER_CRIT) return 'var(--critical)';
    if (pct >= POWER_WARN) return 'var(--warn)';
    return `hsl(265, 60%, ${Math.round(22 + 38 * Math.min(1, pct / POWER_WARN))}%)`;
  }
  if (peakKw <= 0) return 'var(--bg-inset)';
  return `hsl(265, 60%, ${Math.round(22 + 38 * Math.min(1, kw / peakKw))}%)`;
}

export function alarmColor(sev: string, offline: number): string {
  if (offline > 0) return 'var(--critical)';
  switch (sev) {
    case 'CRITICAL': return 'var(--critical)';
    case 'MAJOR': return 'var(--major)';
    case 'MINOR':
    case 'WARNING': return 'var(--warn)';
    default: return 'var(--ok)';
  }
}

/** Used share of the rack's U. A full rack is not a fault, so this is a single
 *  neutral ramp - pale is empty, deep is full - and never a state colour. */
export function spaceColor(freeU: number | null | undefined, uHeight = 42): string {
  if (freeU == null || uHeight <= 0) return 'var(--bg-inset)';
  const used = Math.min(1, Math.max(0, 1 - freeU / uHeight));
  return `hsl(210, 45%, ${Math.round(72 - 44 * used)}%)`;
}

export function rackFill(r: FloorRack, overlay: Overlay, peakKw: number): string {
  if (overlay === 'thermal') return tempColor(r.max_inlet_c);
  if (overlay === 'power') return powerColor(r, peakKw);
  if (overlay === 'space') return spaceColor(r.free_u, r.u_height ?? 42);
  return alarmColor(r.max_severity, r.offline_count);
}

/** A CSS colour as a value WebGL can use: `var(--token)` resolved against the
 *  live theme, anything else passed through. */
export function resolveColor(c: string): string {
  const m = /^var\((--[\w-]+)\)$/.exec(c.trim());
  if (!m) return c;
  const v = getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim();
  return v || '#888888';
}
