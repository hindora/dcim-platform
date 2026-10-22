import type {
  AnomalyParamValue, DetectorLayer, DismissReason, ExclusionSource, FindingKind,
  FindingResolution, FindingState, ParamSpec,
} from '../../../api/client';
import { unitSymbol } from '../../../lib/format';

/** Doc 31 section 10, the strings more than one anomaly surface prints.
 *  Each action has one name everywhere. */

export const LAYER_NAME: Record<DetectorLayer, string> = {
  L0: 'Data quality',
  L2: 'Rule',
  L3: 'Residual against a baseline',
  L4: 'Peer comparison',
  L5: 'Cooling correlator',
};

export const STATE_WORD: Record<FindingState, string> = {
  open: 'Open', acknowledged: 'Acknowledged', confirmed: 'Confirmed',
  dismissed: 'Dismissed', resolved: 'Resolved',
};

export const RESOLUTION_WORD: Record<FindingResolution, string> = {
  cleared: 'condition cleared',
  device_retired: 'device retired',
  detector_disabled: 'detector disabled',
  muted: 'muted by an override',
  superseded: 'superseded by a new detector version',
  manual: 'resolved by hand',
};

export const KIND_WORD: Record<FindingKind, string> = {
  process: 'Process', sensor: 'Sensor', undetermined: 'Undetermined',
};

export const KIND_TIP: Record<FindingKind, string> = {
  process: 'The equipment or process behaves differently from expected.',
  sensor: 'A reading disagrees with its peers or its physics. Check the sensor before the plant.',
  undetermined: 'Not enough redundant readings to tell a sensor problem from a process one.',
};

export const DISMISS_REASONS: { key: DismissReason; label: string; hint: string }[] = [
  { key: 'not_a_fault', label: 'Not a fault',
    hint: 'The equipment is fine; the detector is wrong here.' },
  { key: 'expected_operation', label: 'Expected operation',
    hint: 'Planned work, a setpoint change, staging or a test explains it.' },
  { key: 'sensor_fault', label: 'Sensor problem',
    hint: 'The reading is wrong, not the plant. Fix or recalibrate the sensor.' },
  { key: 'duplicate', label: 'Duplicate',
    hint: 'Another finding or alarm already covers this condition.' },
];

export const EXCLUSION_SOURCE_WORD: Record<ExclusionSource, string> = {
  manual: 'Manual',
  maintenance: 'Maintenance window',
  confirmed_finding: 'Confirmed fault',
  injected_fault: 'Injected fault (simulator)',
};

/** The unit as a reader writes it: "°C" for "celsius", "K" for kelvin. */
export function unitText(unit: string | null | undefined): string {
  return unitSymbol(unit);
}

/** A figure at the precision the reading justifies, in tabular digits. */
export function num(v: number | null | undefined, unit?: string | null): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const a = Math.abs(v);
  const s = a >= 1000 ? v.toFixed(0) : a >= 100 ? v.toFixed(1) : a >= 1 ? v.toFixed(1) : v.toPrecision(2);
  const u = unitText(unit);
  return u ? `${s} ${u}` : s;
}

/** "+3.4 K": signed, with the unit. */
export function signed(v: number | null | undefined, unit?: string | null): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const body = num(Math.abs(v), unit);
  return `${v > 0 ? '+' : v < 0 ? '−' : '±'}${body}`;
}

/** "18 min", "2 h 5 min", "3 d". */
export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 48) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  return `${Math.round(h / 24)} d`;
}

/** A parameter value as the settings page prints it: "3.0 K", "on". */
export function paramText(spec: ParamSpec | undefined, v: AnomalyParamValue | undefined): string {
  if (v === undefined || v === null) return '—';
  if (typeof v === 'boolean') return v ? 'on' : 'off';
  if (typeof v === 'number') {
    const u = unitText(spec?.unit);
    const s = spec?.kind === 'int' ? String(Math.round(v)) : String(v);
    return u ? `${s} ${u}` : s;
  }
  return String(v);
}

/** Why a value is out of bounds, in the copy-deck wording. Null when fine. */
export function rangeError(spec: ParamSpec, v: AnomalyParamValue | undefined): string | null {
  if (spec.kind !== 'number' && spec.kind !== 'int') return null;
  if (typeof v !== 'number' || !Number.isFinite(v)) return 'Enter a number.';
  if (spec.kind === 'int' && !Number.isInteger(v)) return 'Must be a whole number.';
  const lo = spec.min;
  const hi = spec.max;
  const u = unitText(spec.unit);
  if ((lo !== undefined && v < lo) || (hi !== undefined && v > hi)) {
    if (lo !== undefined && hi !== undefined) {
      return `Must be between ${lo} and ${hi}${u ? ` ${u}` : ''}.`;
    }
    return lo !== undefined
      ? `Must be at least ${lo}${u ? ` ${u}` : ''}.`
      : `Must be at most ${hi}${u ? ` ${u}` : ''}.`;
  }
  return null;
}

/** "default 3.0 · 0.5–10": what a field may hold, printed after it. */
export function boundsText(spec: ParamSpec): string {
  const parts = [`default ${paramText(spec, spec.default)}`];
  if (spec.min !== undefined && spec.max !== undefined) parts.push(`${spec.min}–${spec.max}`);
  else if (spec.min !== undefined) parts.push(`at least ${spec.min}`);
  else if (spec.max !== undefined) parts.push(`at most ${spec.max}`);
  return parts.join(' · ');
}

/** `datetime-local` value for an ISO instant, in the browser's zone. */
export function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    + `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** An ISO instant for a `datetime-local` value, or null when empty/invalid. */
export function fromLocalInput(v: string): string | null {
  if (!v) return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

/** "14 Oct 13:30": an absolute stamp, for windows and expiry. */
export function stamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return `${d.toLocaleDateString([], { day: 'numeric', month: 'short' })} `
    + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
