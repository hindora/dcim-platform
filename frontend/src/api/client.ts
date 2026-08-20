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


export interface RackSummary {
  id: string;
  name: string;
  row_name?: string | null;
  room_id?: string | null;
  room_name?: string | null;
  datacenter_code?: string | null;
  u_height: number;
  device_count: number;
  online_count: number;
  offline_count: number;
  load_kw?: number | null;
  rated_power_kw?: number | null;
  load_pct?: number | null;
  max_inlet_c?: number | null;
  max_severity: string;
  free_u?: number | null;
}

export interface ElevationDevice {
  id: string;
  name: string;
  device_type: string;
  status: string;
  health: string;
  max_severity: string;
  power_w?: number | null;
  inlet_temp_c?: number | null;
  cpu_util_pct?: number | null;
}

export interface ElevationSlot {
  u_start: number;
  u_height: number;
  /** Mount side. Null here: the source models rack orientation, not per-device
   *  mounting, and inventing a side would be worse than leaving it blank. */
  facing?: string | null;
  free: boolean;
  device?: ElevationDevice | null;
}

export interface RackElevation {
  rack: RackSummary;
  positions: ElevationSlot[];
  free_blocks: { u_start: number; u_height: number }[];
  /** Vertically mounted PDUs and strapped-on probes: real, but at no U. */
  zero_u_devices: ElevationDevice[];
}


export interface RoomExtent { width_m: number; depth_m: number; derived: boolean }

export interface FloorRack {
  id: string;
  name: string;
  row_name?: string | null;
  x: number;
  y: number;
  /** 'N' faces lower y, 'S' faces higher y. */
  facing?: string | null;
  device_count: number;
  offline_count: number;
  load_kw?: number | null;
  max_inlet_c?: number | null;
  max_severity: string;
  free_u?: number | null;
}

export interface FloorEquipment {
  id: string;
  name: string;
  device_type: string;
  status: string;
  max_severity: string;
  power_w?: number | null;
}

export interface FloorAisle {
  y_start: number;
  y_end: number;
  kind: 'cold' | 'hot' | 'unknown';
  label?: string | null;
  rows: string[];
}

export interface FloorPlan {
  room_id: string;
  room_name: string;
  datacenter_code?: string | null;
  extent: RoomExtent;
  rack_w_m: number;
  rack_d_m: number;
  racks: FloorRack[];
  /** In the room, but with no coordinate to draw it at. */
  unpositioned_equipment: FloorEquipment[];
  aisles: FloorAisle[];
}

export interface RoomSummary {
  id: string;
  name: string;
  datacenter_code?: string | null;
  datacenter_id?: string | null;
}


export interface Termination { type: string; id?: string | null; label?: string | null }

export interface TopologyNode {
  id: string;
  name: string;
  device_type: string;
  status: string;
  max_severity: string;
  /** Hops from the scope anchor. 0 means it was in the requested scope itself
   *  rather than pulled in by traversal. */
  depth: number;
  location: {
    datacenter_code?: string | null;
    room_name?: string | null;
    rack_name?: string | null;
  };
  metrics: Record<string, number>;
}

export interface TopologyEdge {
  id: string;
  source: string;
  target: string;
  layer: string;
  link_type?: string | null;
  redundancy_side?: string | null;
  oper_state: string;
  a_termination: Termination;
  b_termination: Termination;
}

export interface TopologyGraph {
  layer: string;
  scope: string;
  depth: number;
  nodes: TopologyNode[];
  edges: TopologyEdge[];
  truncated: boolean;
  node_count: number;
  edge_count: number;
}


export interface Series {
  metric: string;
  instance: string;
  unit: string;
  /** [epoch_ms, value] pairs. */
  points: number[][];
}

export interface HistoryOut {
  device_id: string;
  /** The bucket actually used - 1m, 5m, 1h or raw. Shown to the reader,
   *  because an averaged hour and a raw sample are not the same claim. */
  interval: string;
  source: string;
  series: Series[];
}

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

  rooms: () => request<{ items: RoomSummary[] }>('/rooms'),

  history: (deviceId: string, metrics: string[], startIso: string, endIso: string) => {
    const q = new URLSearchParams({ start: startIso, end: endIso });
    for (const m of metrics) q.append('metric', m);
    return request<HistoryOut>(`/devices/${deviceId}/history?${q.toString()}`);
  },

  topology: (layer: string, scope: string, depth: number) =>
    request<TopologyGraph>(
      `/topology?layer=${encodeURIComponent(layer)}&scope=${encodeURIComponent(scope)}&depth=${depth}`),

  floorplan: (roomId: string) => request<FloorPlan>(`/rooms/${roomId}/floorplan`),

  rackElevation: (id: string) =>
    request<RackElevation>(`/racks/${id}/elevation`),

  racks: (params: Record<string, string | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    const qs = q.toString();
    return request<{ items: RackSummary[] }>(`/racks${qs ? `?${qs}` : ''}`);
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
