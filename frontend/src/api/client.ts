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

// ------------------------------------------------------- analytics (phase 5)
//
// Narrow on purpose. Every one of these responses carries its own caveats -
// method, category, capacity_source, method_reason - and those fields are typed
// as required rather than optional so a view cannot quietly drop the part that
// says how much the number is worth.

export interface PueResult {
  pue: number | null;
  method: 'energy' | 'power' | null;
  category: 1 | 2 | 3 | null;
  measurement_point?: string;
  plausible: boolean;
  note: string | null;
  total_facility_kwh?: number;
  it_kwh?: number;
  total_facility_kw?: number;
  it_kw?: number;
  counter_resets?: number;
  meters?: { facility: number; it: number };
}

export interface PueSeries {
  points: { start: string; end: string; pue: number | null; method: string | null }[];
  buckets: number;
  mean: number | null;
}

export interface CapacityConstraint {
  name: string;
  unit: string;
  used_p95: number | null;
  used_peak: number | null;
  capacity: number | null;
  capacity_source: string;
  headroom: number | null;
  utilisation_pct: number | null;
  tight: boolean;
  note: string | null;
}

export interface CapacityReport {
  scope: string;
  scope_id: string;
  name: string | null;
  percentile: number;
  window_hours: number;
  binding_constraint: string | null;
  binding_reason: string;
  constraints: CapacityConstraint[];
  notes: string[];
}

export interface ThermalRack {
  rack_id: string;
  name: string;
  inlet_mean_c: number | null;
  exhaust_mean_c: number | null;
  delta_t_k: number | null;
  above_recommended: boolean;
  above_allowable: boolean;
}

export interface ThermalUnit {
  device_id: string;
  name: string;
  state: 'ok' | 'high_supply' | 'high_return' | 'stopped' | string;
  reason: string | null;
  supply_c: number | null;
  return_c: number | null;
  setpoint_c: number | null;
  delta_t_k: number | null;
  running: boolean;
}

export interface ThermalRoom {
  room_id: string;
  name: string | null;
  window_minutes: number;
  inlet_p90_c: number | null;
  hot_spot_threshold_c: number | null;
  hot_spots: { rack_id: string; name: string; inlet_c: number; over_by_k: number }[];
  hot_spot_count: number;
  room_delta_t_k: number | null;
  thermal_event: { type: string; summary: string; hottest?: string } | null;
  crah_units: ThermalUnit[];
  units_high_supply: number;
  units_high_return: number;
  racks: ThermalRack[];
}

export interface CoolingPlant {
  staging: string;
  reason: string;
  load_kw: number;
  running_capacity_kw: number;
  installed_capacity_kw: number;
  running: number;
  standby: number;
  nameplate_unknown: number;
  utilisation_pct: number | null;
  data_quality: { check: string; verdict: string; detail: string }[];
  chillers: { device_id: string; name: string; running: boolean;
              capacity_kw: number | null; load_kw: number | null }[];
  loops: { room_id?: string; name: string; delta_t_k: number | null;
           flow_l_s: number | null; heat_kw: number | null; verdict?: string;
           note?: string }[];
}

export interface PowerFleet {
  redundancy_census: Record<string, number>;
  at_risk: { device_id: string; name: string; device_type: string;
             redundancy: string; reason: string }[];
  at_risk_total: number;
  supplies: { device_id: string; name: string; device_type: string;
              status: string; max_severity: string | null;
              load_pct: number | null; load_w: number | null;
              load_source: string; alternate_feeders: unknown[] }[];
  phase_imbalance: { device_id: string; name: string; imbalance_pct: number }[];
}

export interface ForecastPoint { day: number; value: number; lower: number; upper: number }

export interface ForecastResult {
  scope: string;
  scope_id: string;
  name: string | null;
  metric: string;
  metric_label: string;
  devices: number;
  statistic: string;
  history_days: number;
  min_history_days: number;
  method: 'insufficient_history' | 'linear' | 'holt_winters';
  method_reason: string;
  trend_per_day: number | null;
  r2: number | null;
  unit: string;
  capacity: number | null;
  points: ForecastPoint[];
  history: { day: string; value: number }[];
  runway: {
    days: number | null;
    earliest_days: number | null;
    latest_days: number | null;
    reason: string;
  };
  notes: string[];
}

// ------------------------------------------------- platform self-monitoring
//
// The distinction this whole page exists to make: silence from the datacenter
// and silence from the monitoring look identical on every other screen.

export interface PlatformFinding {
  alarm_type: string;
  instance: string;
  severity: string;
  message: string;
  value: number | null;
  threshold: number | null;
}

export interface CollectorHealth {
  verdict: { healthy: boolean; severity: string | null; summary: string; count?: number };
  findings: PlatformFinding[];
  open_alarms: {
    id: string; alarm_type: string; instance: string; severity: string;
    message: string; first_seen: string; last_seen: string;
    occurrence_count: number; acknowledged_at: string | null;
  }[];
  pipeline: {
    // Two numbers, never one. Freshness is bounded by the poll interval even
    // in perfect health; lag is publish-to-commit and lives under a second.
    ingest_lag_seconds: number | null;
    telemetry_age_seconds: number | null;
    telemetry_present: boolean;
    worker_heartbeat_age_seconds: number | null;
    stream_pending: Record<string, number>;
    lag_warning_seconds: number;
    lag_critical_seconds: number;
  };
  collectors: {
    collector_id: string;
    heartbeat_age_seconds: number | null;
    status: string | null;
    endpoints_owned: number;
    endpoints_online: number;
    stale_after_seconds: number;
  }[];
}

// ------------------------------------------------------------ home page
//
// Alert categories are mutually exclusive and defined server-side in
// app/core/alarm_categories.py. `total` is every open root alarm, so it is a
// superset of the named buckets, not their sum - the home page's ALM indicator
// means "this site has an alarm", which is a different question from "this
// site has a thermal alarm".

export type AlertCategory = 'thermal' | 'connectivity' | 'datapoint' | 'anomaly' | 'other';

export interface AlertCounts {
  total: number;
  thermal: number;
  connectivity: number;
  datapoint: number;
  anomaly: number;
  other: number;
  critical: number;
  major: number;
  minor: number;
}

export interface SiteRoom {
  id: string;
  name: string;
  room_type: string;
  floor?: string | null;
  datacenter_id: string;
  datacenter_code: string;
  rack_count: number;
  device_count: number;
  offline_count: number;
  alerts: AlertCounts;
}

export interface SiteRow {
  id: string;
  code: string;
  name: string;
  /** Null until the datacenter record is seeded; rendered as "not set". */
  city?: string | null;
  country?: string | null;
  timezone: string;
  room_count: number;
  device_count: number;
  online_count: number;
  offline_count: number;
  alerts: AlertCounts;
  rooms: SiteRoom[];
}

export interface SitesOverview {
  sites: SiteRow[];
  totals: AlertCounts;
  /** Open root alarms that resolve to no site - platform alarms such as
   *  `ingest_stalled`. Counted in `totals`, absent from every row, so the page
   *  says so rather than leaving the columns not adding up. */
  unlocated_alerts: number;
  as_of: string;
}

/** A number the platform may legitimately not have. `note` says why not. */
export interface MaybeMetric {
  value: number | null;
  note?: string | null;
  method?: string | null;
  category?: number | null;
}

export interface Utilisation {
  pct: number | null;
  basis?: string | null;
  note?: string | null;
}

export interface SiteKpi {
  site: {
    id: string; code: string; name: string;
    city?: string | null; country?: string | null; timezone: string;
    design_it_kw: number | null; design_pue: number | null;
  };
  monitored: {
    devices: number; devices_online: number; devices_offline: number;
    endpoints: number; endpoints_enabled: number; protocols: number;
    racks: number;
  };
  efficiency: {
    pue: MaybeMetric; cer: MaybeMetric; wue: MaybeMetric; cue: MaybeMetric;
  };
  power: {
    total_kw: number; it_load_kw: number; cooling_kw: number;
    facility_other_kw: number; reporting_devices: number;
  };
  utilisation: { power: Utilisation; space: Utilisation; cooling: Utilisation };
  weather: {
    available: boolean;
    note?: string | null;
    /** Read off the cooling tower controller over BACnet. */
    source?: string | null;
    dry_bulb_c: number | null;
    wet_bulb_c: number | null;
    /** Neither is instrumented at any site; present so absence is explicit. */
    humidity_pct: number | null;
    wind_speed_ms: number | null;
    as_of?: string | null;
    /** Slow-polled points, so age travels with the value. */
    age_s?: number | null;
  };
  alerts: AlertCounts;
  as_of: string;
}

/* ------------------------------------------------------------------ estate
 *
 * Thermal, power and utilisation share one row shape so a single table can
 * render all three. Every optional number is genuinely optional: `null` means
 * nothing measured it, which the UI must show as a dash and a reason rather
 * than as a zero.
 */

export interface EstateRowBase {
  id: string;
  kind: 'site' | 'room';
  name: string;
  site_id: string;
  site_code: string;
  site_name: string;
  floor?: string | null;
  room_type?: string | null;
  room_count?: number;
  rack_count?: number;
  note?: string | null;
}

export interface ThermalRow extends EstateRowBase {
  avg_c: number | null;
  max_c: number | null;
  compliance_pct: number | null;
  samples: number;
  delta_avg: number | null;
  delta_max: number | null;
}

export interface ThermalPage {
  window: {
    mode: string; label: string; compare_label: string;
    focus_start: string; focus_end: string;
  };
  band: { low_c: number; high_c: number; basis: string };
  totals: {
    avg_c: number | null; max_c: number | null; compliance_pct: number | null;
    samples: number; rooms_reporting: number; rooms: number;
  };
  sites: ThermalRow[];
  rooms: ThermalRow[];
  notes: string[];
}

export interface PowerRow extends EstateRowBase {
  total_kw: number | null;
  it_ac_kw: number | null;
  /** No DC bus is metered in this estate; always null, rendered as a dash. */
  it_dc_kw: number | null;
  cooling_kw: number | null;
  other_kw: number | null;
  pue: number | null;
  delta_total: number | null;
}

export interface PowerPage {
  window: {
    mode: string; label?: string; start?: string; end?: string;
    bucket_seconds?: number;
  };
  totals: {
    total_kw: number | null; it_ac_kw: number | null; it_dc_kw: number | null;
    cooling_kw: number | null; other_kw: number | null; pue: number | null;
    rooms_reporting: number; rooms: number;
  };
  sites: PowerRow[];
  rooms: PowerRow[];
  notes: string[];
}

export interface UtilRow extends EstateRowBase {
  space_pct: number | null;
  space_used_u: number;
  space_total_u: number;
  power_pct: number | null;
  power_used_kw: number;
  power_capacity_kw: number | null;
  /** Which denominator was available - a design rating, or summed nameplate. */
  power_basis: string;
  cooling_pct: number | null;
  cooling_used_kw: number;
  cooling_capacity_kw: number | null;
  cooling_basis: string;
}

export interface UtilPage {
  totals: {
    space_pct: number | null; power_used_kw: number;
    cooling_capacity_kw: number | null; cooling_pct: number | null;
    racks: number; rooms: number;
  };
  sites: UtilRow[];
  rooms: UtilRow[];
  notes: string[];
}

export interface AlertDrill {
  category: string;
  rows: Array<{
    room_id: string; room_name: string; floor: string | null;
    site_id: string; site_code: string; site_name: string;
    qty: number; devices: number; critical: number; major: number;
  }>;
  total: number;
  /** Platform alarms belong to no room; counted, never silently dropped. */
  unlocated: number;
}

export interface RoomKpi {
  room: {
    id: string; name: string; room_type: string | null; floor: string | null;
    design_it_kw: number | null; datacenter_id: string;
    site_code: string; site_name: string; city: string | null;
  };
  monitored: {
    devices: number; online: number; offline: number; racks: number;
    cooling_units: number; cooling_online: number;
    power_units: number; power_online: number;
  };
  environmental: {
    avg_c: number | null; max_c: number | null; compliance_pct: number | null;
    band: { low_c: number; high_c: number };
    note: string | null; humidity_note: string;
  };
  power: {
    total_kw: number | null; it_ac_kw: number | null; it_dc_kw: number | null;
    cooling_kw: number | null; pue: number | null; note: string | null;
  };
  utilisation: {
    space_pct: number | null; power_pct: number | null; power_basis: string;
    cooling_pct: number | null; cooling_basis: string;
  };
  last_sample: string | null;
  as_of: string;
}

export const api = {
  login: (username: string, password: string) =>
    request<{ token: string; expires_in: number; username: string; role: string }>(
      '/login',
      { method: 'POST', body: JSON.stringify({ username, password }) },
    ),

  dashboard: () => request<DashboardSummary>('/dashboard/summary'),

  /** One call behind the entire home page - table, tabs and alert strip. */
  sitesOverview: () => request<SitesOverview>('/sites/overview'),

  siteKpi: (datacenterId: string) =>
    request<SiteKpi>(`/sites/${datacenterId}/kpi`),

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

  pue: (params: Record<string, string | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    const qs = q.toString();
    return request<PueResult>(`/analytics/pue${qs ? `?${qs}` : ''}`);
  },

  pueSeries: (params: Record<string, string | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    const qs = q.toString();
    return request<PueSeries>(`/analytics/pue/series${qs ? `?${qs}` : ''}`);
  },

  capacity: (scope: string, scopeId: string, assumedRackKw?: number) => {
    const q = new URLSearchParams({ scope, scope_id: scopeId });
    if (assumedRackKw) q.set('assumed_rack_kw', String(assumedRackKw));
    return request<CapacityReport>(`/capacity?${q.toString()}`);
  },

  thermal: (roomId: string, minutes?: number) => {
    const q = new URLSearchParams({ room_id: roomId });
    if (minutes) q.set('minutes', String(minutes));
    return request<ThermalRoom>(`/analytics/thermal?${q.toString()}`);
  },

  cooling: (datacenterId?: string) => {
    const q = new URLSearchParams();
    if (datacenterId) q.set('datacenter_id', datacenterId);
    const qs = q.toString();
    return request<CoolingPlant>(`/cooling${qs ? `?${qs}` : ''}`);
  },

  powerFleet: (datacenterId?: string) => {
    const q = new URLSearchParams();
    if (datacenterId) q.set('datacenter_id', datacenterId);
    const qs = q.toString();
    return request<PowerFleet>(`/power${qs ? `?${qs}` : ''}`);
  },

  forecast: (scope: string, scopeId: string, opts: {
    metric?: string; horizonDays?: number; historyDays?: number; capacity?: number;
  } = {}) => {
    const q = new URLSearchParams({ scope, scope_id: scopeId });
    if (opts.metric) q.set('metric', opts.metric);
    if (opts.horizonDays) q.set('horizon_days', String(opts.horizonDays));
    if (opts.historyDays) q.set('history_days', String(opts.historyDays));
    if (opts.capacity) q.set('capacity', String(opts.capacity));
    return request<ForecastResult>(`/analytics/forecast?${q.toString()}`);
  },

  /** Estate thermal: one request serves both the SITES and ROOMS scopes. */
  estateThermal: (params: { focus?: string; compare?: string; mode?: string } = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    const qs = q.toString();
    return request<ThermalPage>(`/estate/thermal${qs ? `?${qs}` : ''}`);
  },

  estatePower: (params: {
    start?: string; end?: string; mode?: string; live?: boolean;
  } = {}) => {
    const q = new URLSearchParams();
    if (params.start) q.set('start', params.start);
    if (params.end) q.set('end', params.end);
    if (params.mode) q.set('mode', params.mode);
    if (params.live) q.set('live', 'true');
    const qs = q.toString();
    return request<PowerPage>(`/estate/power${qs ? `?${qs}` : ''}`);
  },

  estateUtilization: () => request<UtilPage>('/estate/utilization'),

  estateAlerts: (category: string) =>
    request<AlertDrill>(`/estate/alerts?category=${encodeURIComponent(category)}`),

  roomKpi: (roomId: string) => request<RoomKpi>(`/estate/rooms/${roomId}/kpi`),

  collectorHealth: () => request<CollectorHealth>('/collector/health'),

  wsTicket: () =>
    request<{ ticket: string; expires_in: number }>('/ws/ticket', {
      method: 'POST', body: '{}',
    }),
};
