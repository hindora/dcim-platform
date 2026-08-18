// Unit formatting driven by the generated metric registry.
//
// No component ever hardcodes a unit string. When a metric's unit changes -
// which, per the registry rules, means a NEW metric key - nothing in the UI
// needs editing.

import { METRICS, type MetricKey } from './metrics.gen';

const SI = [
  { limit: 1e9, suffix: 'G' },
  { limit: 1e6, suffix: 'M' },
  { limit: 1e3, suffix: 'k' },
];

const BINARY = [
  { limit: 1024 ** 4, suffix: 'TiB' },
  { limit: 1024 ** 3, suffix: 'GiB' },
  { limit: 1024 ** 2, suffix: 'MiB' },
  { limit: 1024, suffix: 'KiB' },
];

function round(v: number, digits = 1): string {
  return Number.isInteger(v) ? String(v) : v.toFixed(digits);
}

export function formatValue(unit: string, value: number): string {
  switch (unit) {
    case 'C':
      return `${round(value)} °C`;
    case 'pct':
      return `${round(value)}%`;
    case 'ratio':
      return round(value, 2);
    case 'B': {
      for (const { limit, suffix } of BINARY) {
        if (Math.abs(value) >= limit) return `${round(value / limit)} ${suffix}`;
      }
      return `${round(value, 0)} B`;
    }
    case 'W':
    case 'V':
    case 'A':
    case 'Hz':
    case 'bps':
    case 'count':
    case 's': {
      for (const { limit, suffix } of SI) {
        if (Math.abs(value) >= limit) return `${round(value / limit)} ${suffix}${unit}`;
      }
      return `${round(value)} ${unit}`;
    }
    default:
      return `${round(value)} ${unit}`;
  }
}

export function formatMetric(key: string, value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  const def = METRICS[key as MetricKey];
  if (typeof value !== 'number') return String(value);
  if (!def) return round(value);
  return formatValue(def.unit, value);
}

export function metricLabel(key: string): string {
  return METRICS[key as MetricKey]?.displayName ?? key;
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return 'never';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return 'never';
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

// Status classes. Colour is never the only signal - callers pair this with a
// glyph or the status text itself.
export function statusClass(status: string): string {
  switch (status) {
    case 'ONLINE':
    case 'OK':
    case 'CLEAR':
      return 'ok';
    case 'DEGRADED':
    case 'WARNING':
      return 'warn';
    case 'MINOR':
    case 'MAJOR':
      return 'major';
    case 'OFFLINE':
    case 'CRITICAL':
      return 'critical';
    default:
      return 'unknown';
  }
}
