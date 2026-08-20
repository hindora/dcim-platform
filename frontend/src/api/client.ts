// Typed API client. Generated types would come from contracts/openapi in a
// later phase; these mirror the Pydantic response models by hand for now and
// are deliberately narrow - a component that needs a new field should force a
// change here rather than reach into `any`.

const TOKEN_KEY = 'dcim.token';

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (init.body) headers.set('Content-Type', 'application/json');

  const res = await fetch(`/api/v1${path}`, { ...init, headers });

  if (res.status === 401) {
    // An expired token must not leave the UI showing stale data behind a
    // silently failing refresh loop.
    setToken(null);
    throw new ApiError(401, 'session expired');
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? body.title ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---------------------------------------------------------------- types

export interface LocationRef {
  datacenter_code?: string | null;
  room_name?: string | null;
  row_name?: string | null;
  rack_id?: string | null;
  rack_name?: string | null;
  u_start?: number | null;
}

export interface DeviceSummary {
  id: string;
  name: string;
  device_type: string;
  vendor?: string | null;
  model?: string | null;
  status: string;
  health: string;
  max_severity: string;
  mgmt_ip?: string | null;
  primary_ip?: string | null;
  last_seen?: string | null;
  location: LocationRef;
}

export interface EndpointSummary {
  id: string;
  protocol: string;
  role: string;
  address?: string | null;
  port?: number | null;
  enabled: boolean;
  credential_hint?: string | null;
  poll_interval_s?: number | null;
  status: string;
  /** Every poll attempt. Fresh here with a stale last_success = polling and failing. */
  last_seen?: string | null;
  last_success?: string | null;
  last_error?: string | null;
  last_error_class?: string | null;
  consecutive_failures: number;
  last_latency_ms?: number | null;
  /** Lifetime totals, not a recent window - the collector resets on restart. */
  poll_count: number;
  fail_count: number;
  timeout_count: number;
  auth_fail_count: number;
}

export interface DeviceDetail extends DeviceSummary {
  serial_number?: string | null;
  u_height: number;
  lifecycle: string;
  admin_state: string;
  attributes: Record<string, unknown>;
  endpoints: EndpointSummary[];
}

export interface MetricValue {
  v: number | boolean | string | null;
  u?: string | null;
  t?: string | null;
  q: string;
}

export interface DeviceState {
  device_id: string;
  status: string;
  health: string;
  max_severity: string;
  active_alarms: number;
  last_seen?: string | null;
  metrics: Record<string, MetricValue>;
}

export interface DashboardSummary {
  devices: {
    total: number; online: number; offline: number;
    degraded: number; unknown: number;
  };
  power: Record<string, number | null>;
  environment: Record<string, number | null>;
  collectors: Array<{
    id: string; status: string; version?: string | null;
    endpoints_owned: number; endpoints_online: number;
    last_heartbeat?: string | null;
  }>;
  ingest: { newest_sample?: string | null; lag_seconds?: number | null };
  as_of: string;
}

export interface Alarm {
  id: string;
  device_id: string;
  device_name: string;
  device_type: string;
  alarm_type: string;
  instance: string;
  severity: string;
  state: string;
  message: string;
  metric_key?: string | null;
  trigger_value?: number | null;
  threshold?: number | null;
  source: string;
  first_seen: string;
  last_seen: string;
  occurrence_count: number;
  is_symptom: boolean;
  datacenter_code?: string | null;
  room_name?: string | null;
  rack_name?: string | null;
}

export interface AlarmSummary {
  active: number;
  critical: number;
  major: number;
  warning: number;
  acknowledged: number;
  suppressed_symptoms: number;
}

export interface EventItem {
  id: number;
  ts: string;
  device_id?: string | null;
  device_name?: string | null;
  source_ip?: string | null;
  event_type: string;
  source: string;
  severity: string;
  message: string;
}

export interface Page<T> {
  items: T[];
  next_cursor?: string | null;
  total?: number | null;
}

// ------------------------------------------------------------- endpoints

export const api = {
  login: (username: string, password: string) =>
    request<{ token: string; expires_in: number; username: string; role: string }>(
      '/login',
      { method: 'POST', body: JSON.stringify({ username, password }) },
    ),

  dashboard: () => request<DashboardSummary>('/dashboard/summary'),

  devices: (params: Record<string, string | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    const qs = q.toString();
    return request<Page<DeviceSummary>>(`/devices${qs ? `?${qs}` : ''}`);
  },

  device: (id: string) => request<DeviceDetail>(`/devices/${id}`),

  deviceState: (id: string) => request<DeviceState>(`/devices/${id}/state`),

  racks: (params: Record<string, string | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    const qs = q.toString();
    return request<{ items: unknown[] }>(`/racks${qs ? `?${qs}` : ''}`);
  },

  collectors: () => request<{ items: unknown[] }>('/collector/instances'),

  alarms: (params: Record<string, string | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    const qs = q.toString();
    return request<{ items: Alarm[] }>(`/alarms${qs ? `?${qs}` : ''}`);
  },

  alarmSummary: () => request<AlarmSummary>('/alarms/summary'),

  acknowledgeAlarm: (id: string) =>
    request<{ ok: boolean }>(`/alarms/${id}/acknowledge`, {
      method: 'POST', body: JSON.stringify({}),
    }),

  clearAlarm: (id: string) =>
    request<{ ok: boolean }>(`/alarms/${id}/clear`, { method: 'POST', body: '{}' }),

  events: (params: Record<string, string | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    const qs = q.toString();
    return request<{ items: EventItem[] }>(`/events${qs ? `?${qs}` : ''}`);
  },

  wsTicket: () =>
    request<{ ticket: string; expires_in: number }>('/ws/ticket', {
      method: 'POST', body: '{}',
    }),
};
