// Typed API client. Generated types would come from contracts/openapi in a
// later phase; these mirror the Pydantic response models by hand for now and
// are deliberately narrow - a component that needs a new field should force a
// change here rather than reach into `any`.

const TOKEN_KEY = 'dcim.token';

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

/** Told when the session ends underneath whatever the user was doing.
 *
 *  Clearing the token is not enough on its own: the app decides once, at
 *  mount, whether it is signed in, so a token dropped by a 401 left the shell
 *  standing over data it could no longer refresh. Every page then rendered its
 *  own phrasing of one global cause - eighteen of them - and none could be
 *  acted on, because the only fix was to sign in again.
 */
type AuthListener = () => void;
const authLost = new Set<AuthListener>();

export function onAuthLost(fn: AuthListener): () => void {
  authLost.add(fn);
  return () => { authLost.delete(fn); };
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
    authLost.forEach((fn) => fn());
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
  /** Asset-view fields. Optional and additive: the Devices pages ignore them,
   *  which is what keeps them unchanged while /assets is built. */
  asset_tag?: string | null;
  serial_number?: string | null;
  lifecycle?: string;
  category?: string | null;
  warranty_expires?: string | null;
  /** active | expiring | expired | unknown, derived server-side. */
  warranty_state?: string;
  owner_group?: string | null;
  cost_centre?: string | null;
  tags?: { id: string; key: string; value: string; colour?: string | null }[];
}

/** Estate-wide counts behind the /assets landing page.
 *
 *  Blocks for tables that are not migrated yet - warranty, maintenance, parts -
 *  are absent rather than zero. Optional here for the same reason: the UI must
 *  render "not tracked yet" rather than a confident nought.
 */
export interface AssetSummary {
  totals: {
    assets: number;
    planned: number;
    in_stock: number;
    installed: number;
    in_service: number;
    maintenance: number;
    decommissioned: number;
    retired: number;
  };
  identity: {
    with_serial: number;
    with_asset_tag: number;
    unidentified: number;
  };
  estate: {
    datacenters: number;
    rooms: number;
    racks: number;
    u_total: number;
    u_used: number;
    u_reserved: number;
    u_free: number;
  };
  by_category: { category: string; n: number }[];
  discovery: { new_candidates: number; unmatched: number };
  /** Absent until migration 0047 - a tile reading "0 expiring" with no
   *  contract table is a statement an operator would act on, and false. */
  warranty?: { unknown: number; expired: number; expiring: number; active: number };
  contracts?: { total: number; expired: number; expiring: number };
  expiring_days?: number;
}

/** One device on a power path, upstream of the load. */
export interface PowerHop {
  device_id: string;
  name: string;
  device_type: string;
  status: string;
  max_severity: string;
  load_pct?: number | null;
  load_w?: number | null;
  load_source?: string | null;
  /** Other devices that also feed this hop; named rather than dropped. */
  alternate_feeders: string[];
}

export interface PowerPath {
  /** 'A' or 'B' on a dual-corded load; null when the cord carries no side. */
  side?: string | null;
  hops: PowerHop[];
  reaches_source: boolean;
}

/** What feeds one load, and whether it is still redundant. */
export interface PowerChain {
  device: PowerHop;
  redundancy: string;
  reason: string;
  live_paths: number;
  total_paths: number;
  paths: PowerPath[];
  shared_upstream: PowerHop[];
}

/** A responder a sweep found, and whether inventory already claims it. */
export interface DiscoveryCandidate {
  id: string;
  run_id: string;
  address?: string | null;
  protocol: string;
  /** Whatever the probe could read: sysDescr, sysObjectID, sysName. */
  identity: Record<string, unknown>;
  suggested_device_type?: string | null;
  suggested_vendor?: string | null;
  suggested_model?: string | null;
  /** Non-null means this responder is already known. */
  matched_device_id?: string | null;
  matched_device_name?: string | null;
  status: string;
  first_seen: string;
  last_seen: string;
}

export interface LifecycleEvent {
  id: string;
  from_state?: string | null;
  to_state: string;
  reason?: string | null;
  change_ref?: string | null;
  actor: string;
  ts: string;
}

export interface LifecycleHistory {
  current?: string | null;
  /** What this device may move to next. Server-owned: the UI must never offer
   *  a transition the matrix would refuse. */
  allowed: string[];
  events: LifecycleEvent[];
}

export interface MaintenanceWindow {
  id: string;
  title: string;
  description?: string | null;
  change_ref?: string | null;
  kind: string;
  starts_at: string;
  ends_at: string;
  status: string;
  suppress: boolean;
  created_by: string;
  target_count: number;
  /** Alarms this window is holding out of the active list. The number that
   *  tells an operator the window was scoped too widely. */
  shelved_alarms: number;
  targets?: { id: string; name: string; device_type: string; max_severity: string }[];
  shelved?: {
    id: string; alarm_type: string; severity: string; state: string;
    message: string; first_seen: string; device_name: string; device_id: string;
  }[];
}

export interface MaintenancePreview {
  devices: number;
  downstream_devices: number;
  /** Goes dark, as opposed to merely losing a redundant feed. */
  cut_off: number;
  alarms_currently_active: number;
  redundancy_warnings: { device_id: string; redundancy: string; reason: string }[];
}

export interface MaintenanceRecord {
  id: string;
  window_id?: string | null;
  window_title?: string | null;
  performed_at: string;
  performed_by: string;
  kind: string;
  summary: string;
  detail?: string | null;
}

export interface Supplier {
  id: string;
  name: string;
  account_ref?: string | null;
  contact_name?: string | null;
  contact_email?: string | null;
  device_count: number;
  contract_count: number;
}

export interface SupportContract {
  id: string;
  supplier_id?: string | null;
  supplier_name?: string | null;
  reference: string;
  kind: string;
  service_level?: string | null;
  start_date: string;
  end_date: string;
  cost?: number | null;
  currency?: string | null;
  auto_renew: boolean;
  notes?: string | null;
  device_count: number;
  /** Derived server-side from one threshold: active | expiring | expired. */
  state: string;
  days_remaining: number;
  devices?: { id: string; name: string; device_type: string;
              serial_number?: string | null; warranty_expires?: string | null }[];
}

export interface Tag {
  id: string;
  key: string;
  value: string;
  colour?: string | null;
  description?: string | null;
  usage_count: number;
}

export interface AssetFilterOptions {
  device_types: {
    code: string;
    display_name: string;
    category: string;
    is_rack_mounted: boolean;
    device_count: number;
  }[];
  vendors: { id: string; name: string; device_count: number }[];
  lifecycles: { value: string; label: string }[];
}

export interface CredentialSummary {
  id: string;
  name: string;
  protocol: string;
  kind: string;
  /** A hint only. The secret never leaves the server. */
  secret_hint?: string | null;
  rotated_at?: string | null;
  endpoints: number;
}

export interface PollProfileSummary {
  id: string;
  name: string;
  interval_s: number;
  timeout_ms: number;
  retries: number;
  push_enabled: boolean;
  metric_groups: string[];
  endpoints: number;
  /** Present in the profiles list, absent in the endpoint-editor picker. */
  endpoints_enabled?: number;
  protocols?: string[];
}

export interface ConfigField {
  key: string;
  label: string;
  /** One short line, always on screen. */
  help: string;
  /** The reasoning - why a limit is where it is, what breaks if it is wrong.
   *  Read once by whoever decides to touch a setting, so it lives in a tooltip
   *  rather than on the page. */
  detail?: string;
  /** 'int' | 'seconds' | 'bool' | 'text' | 'listen' */
  kind: string;
  /** 'live' applies without a restart; 'restart' is stored until the next one. */
  when: 'live' | 'restart';
  min?: number;
  max?: number;
}

export interface ConfigSection {
  key: string;
  title: string;
  /** Set on sections that own a listener: moving one silences a plane with no
   *  error anywhere, because every device keeps sending to the old address. */
  danger?: string | null;
  fields: ConfigField[];
}

export interface CollectorRow {
  id: string;
  hostname?: string | null;
  build?: string | null;
  status: string;
  started_at?: string | null;
  last_heartbeat?: string | null;
  endpoints_owned: number;
  endpoints_online: number;
  config: Record<string, Record<string, unknown>>;
  /** What the collector reports it is actually running: its own file's values
   *  with any override already folded in. Empty until it has heartbeated once,
   *  and the platform cannot derive it - the defaults live in collector.yaml
   *  on the collector's host. */
  effective: Record<string, Record<string, unknown>>;
  /** What is stored here. */
  version: number;
  /** What the collector's last heartbeat said it is actually running. */
  running_version: number;
  restart_pending: boolean;
  config_error?: string | null;
  alive: boolean;
  updated_at?: string | null;
  updated_by?: string | null;
}

export interface CollectorsPage {
  collectors: CollectorRow[];
  schema: { sections: ConfigSection[] };
}

export interface ProfileUsage {
  profile_id: string;
  endpoints: number;
  devices: number;
  breakdown: { protocol: string; device_type: string;
               endpoints: number; devices: number }[];
}

export interface PollProfilesPage {
  profiles: PollProfileSummary[];
  /** Group names each protocol's mapping file defines. SNMP is the only key:
   *  it is the only adapter that reads metric_groups. */
  metric_groups: Record<string, string[]>;
  limits: {
    min_interval_s: number; max_interval_s: number;
    min_timeout_ms: number; max_timeout_ms: number; max_retries: number;
  };
}

export interface PollProfileBody {
  name?: string;
  interval_s?: number;
  timeout_ms?: number;
  retries?: number;
  metric_groups?: string[];
  push_enabled?: boolean;
}

export interface AddressingField {
  label: string;
  help?: string;
  /** Absent means a bounded integer - a unit ID, a network number. */
  kind?: 'text' | 'bool' | 'choice';
  choices?: string[];
  min?: number;
  max?: number;
}

export interface EndpointOptions {
  credentials: CredentialSummary[];
  /** How many exist behind the capped list, so the picker can say which it is
   *  showing rather than silently offering the first fifty of nine hundred. */
  credential_total: number;
  poll_profiles: PollProfileSummary[];
  default_ports: Record<string, number>;
  addressing: Record<string, Record<string, AddressingField>>;
}

export interface EndpointPatch {
  address?: string | null;
  port?: number | null;
  addressing?: Record<string, string | number | boolean>;
  credential_id?: string | null;
  poll_profile_id?: string | null;
  enabled?: boolean;
  admin_state?: string;
}

export interface EndpointSummary {
  id: string;
  protocol: string;
  role: string;
  address?: string | null;
  port?: number | null;
  enabled: boolean;
  admin_state: string;
  /** Protocol-specific selectors: a Modbus unit ID, a BACnet device instance.
   *  Behind a gateway this, not the address, decides which device answers. */
  addressing: Record<string, string | number | boolean>;
  credential_id?: string | null;
  credential_name?: string | null;
  credential_hint?: string | null;
  poll_profile_id?: string | null;
  poll_profile_name?: string | null;
  /** Set when the endpoint is reached THROUGH another one - a serial gateway
   *  or a BACnet router. Its address belongs to the gateway, not to it. */
  via_endpoint_id?: string | null;
  via_name?: string | null;
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

/** One physical network port, as built and as cabled. */
export interface NetworkInterface {
  id: string;
  if_index?: number | null;
  name: string;
  /** data or mgmt. A BMC port and a data NIC are different networks that fail
   *  independently, which is why the distinction is carried rather than
   *  inferred from the port name. */
  role: string;
  speed_bps?: number | null;
  ip?: string | null;
  mac?: string | null;
  admin_state: string;
  /** The far end of the cable, when there is one. Null means a spare port. */
  peer_device_id?: string | null;
  peer_device?: string | null;
  peer_port?: string | null;
  peer_layer?: string | null;
}

export interface PowerSupply {
  number: number;
  connector?: string | null;
  rated_watts?: number | null;
  /** The outlet this cord lands on. Null means fitted but not corded. */
  feed_device_id?: string | null;
  feed_device?: string | null;
  feed_outlet?: number | null;
}

export interface DeviceDetail extends DeviceSummary {
  // serial_number, asset_tag, lifecycle and category are inherited: they were
  // promoted onto DeviceSummary so the asset list can render them per row.
  /** The MODEL's datasheet rating, not a reading - what the chassis is built
   *  to draw at most, and what a feed is sized against. */
  rated_power_w?: number | null;
  psus: PowerSupply[];
  u_height: number;
  /** Which way the chassis faces in the rack. */
  facing?: string | null;
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
  /** When it closed. Present on the row all along - the panel simply had no
   *  way to ask for closed conditions until there was a lifecycle filter. */
  cleared_at?: string | null;
  is_symptom: boolean;
  datacenter_code?: string | null;
  room_name?: string | null;
  rack_name?: string | null;
  /** What is plugged into the receptacle `instance` names, when it names one.
   *  "Outlet 31" says where to put your hand; this says what you are about to
   *  unplug. Null for every condition that is not about one outlet. */
  instance_feeds?: string | null;
  /** The absolute load behind a percentage measurement, in VOLT-AMPS, computed
   *  from the value captured at raise and the device nameplate. Apparent, not
   *  real: a PDU nameplate is VA and load% is a share of it. Null unless the
   *  alarm measured `load_pct`. */
  trigger_va?: number | null;
  /** Stamped at raise time, not derived on read - see the taxonomy doc. */
  category?: AlarmCategory | null;
  detection?: AlarmDetection | null;
  response_class?: ResponseClass | null;
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
// The home page is an ALARM console: every count it receives is
// `response_class = 'alarm'`, a condition that requires a response now.
// Informational alerts - wear, hygiene, stale telemetry - are classified and
// stored, and are not on this screen. Reach them with
// `/alarms?response_class=alert`.
//
// Categories are one axis - the domain of the failing thing, which is the same
// question as who owns the first five minutes - defined server-side in
// app/core/alert_taxonomy.py. They are mutually exclusive, so `by_category`
// sums to `total`.
//
// `by_detection` is deliberately NOT a category. How a condition was found
// (threshold, state, absence, derived, forecast) filters across all eight, so
// improving a detector never moves an alarm between buckets.

export type AlarmCategory =
  | 'visibility' | 'environmental' | 'cooling' | 'power'
  | 'it_equipment' | 'network' | 'capacity';

export type AlarmDetection =
  | 'threshold' | 'state' | 'absence' | 'derived' | 'forecast';

/** Required response, the ISA-18.2 split. An `alarm` demands action now and
 *  expects an acknowledgement; an `alert` is informational and belongs to
 *  whoever schedules the work.
 *
 *  The home page shows alarms only, so this appears in the legend - which has
 *  to be able to say what the console leaves out - rather than on any counter. */
export type ResponseClass = 'alarm' | 'alert';

export interface AlarmCounts {
  /** Open root ALARMS - what needs a response now. The ALARMS counter and the
   *  ALM column read this, and critical/major/minor describe it. */
  total: number;
  /** Every open root condition, alarms and alerts. This is what the seven
   *  category counters sum to; it is NOT `total` plus anything. */
  open_total: number;
  critical: number;
  major: number;
  minor: number;
  /** Every open condition in each domain - the domain counters. */
  by_category: Record<AlarmCategory, number>;
  /** The actionable subset of each domain, so a tile can be coloured by
   *  whether anything in it must be answered. */
  by_category_alarms: Record<AlarmCategory, number>;
  by_detection: Record<AlarmDetection, number>;
}

/** The taxonomy itself, served by the classifier that fills the counters.
 *  Fetched rather than transcribed: a legend written next to the classifier
 *  drifts from it, and the first symptom is an operator routing work by a
 *  definition that stopped being true. */
export interface AlarmTaxonomy {
  categories: {
    key: AlarmCategory;
    label: string;
    /** Who acts first. The reason the axis is worth having. */
    owner: string;
    description: string;
    examples: string[];
  }[];
  /** How the eight group into the headline counters on the strip. The table
   *  keeps a column per category, so the grouping loses nothing. */
  strip_groups: { key: string; label: string; categories: AlarmCategory[] }[];
  detections: { key: AlarmDetection; label: string; description: string }[];
  /** Alarm and alert, defined. The legend uses these to say what this console
   *  shows and what it deliberately does not. */
  response_classes: { key: ResponseClass; label: string; description: string }[];
  /** Every condition the platform can raise, assembled from the rules table,
   *  the plant's own fault points and the classifier. `response_class` is null
   *  where severity is not fixed until the condition arrives - a trap carries
   *  the severity the device chose. */
  conditions: {
    key: string;
    label: string;
    /** Null when the category depends on the equipment reporting it: the same
     *  fan speed is cooling on a CRAH and IT on a server. */
    category: AlarmCategory | null;
    origin: 'rule' | 'equipment' | 'reported' | 'platform' | 'planned';
    severity: string | null;
    response_class: ResponseClass | null;
    enabled: boolean;
    detail: string;
  }[];
  origins: { key: string; label: string; text: string }[];
  summary: {
    total: number; alarm: number; alert: number; varies: number;
    disabled: number; planned: number;
  };
}

export interface SiteRoom {
  id: string;
  name: string;
  room_type: string;
  /** 'white_space' where racks live, 'facility' for plant and switchrooms. */
  room_class: string | null;
  floor?: string | null;
  datacenter_id: string;
  datacenter_code: string;
  rack_count: number;
  device_count: number;
  offline_count: number;
  alarms: AlarmCounts;
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
  alarms: AlarmCounts;
  rooms: SiteRoom[];
}

/** Identity for the shell. `org_name` is whose estate this is - the product's
 *  own name is the same on every install and tells an operator with two tabs
 *  open nothing about which one they are acknowledging an alarm on. */
export interface Instance {
  org_name: string;
  environment: string;
}

/** The state of the monitoring itself - deliberately NOT part of the estate's
 *  counters.
 *
 *  Location decides: a condition in a room is the estate's, a condition in the
 *  pipeline is the platform's. That keeps the estate figure equal to the sum
 *  of the site rows, and it puts the pipeline where an operator can see what it
 *  has done to everything else on the page - which a "+2" on an alarm count
 *  never said. */
export interface PlatformState {
  /** ok · degraded (something informational) · impaired (an alarm) ·
   *  blind (critical, or the data has stopped arriving). */
  state: 'ok' | 'degraded' | 'impaired' | 'blind';
  alarms: number;
  alerts: number;
  /** Age of the newest sample in the estate. `null` means none has ever
   *  landed, which is a different claim from "the newest one is old". */
  telemetry_age_s: number | null;
  /** The age past which the platform stops vouching for its own numbers.
   *  Sent so the UI cannot disagree with the badge about what stale means. */
  telemetry_trusted_s: number;
  telemetry_stale: boolean;
  conditions: {
    alarm_type: string; instance: string; severity: string;
    response_class: string; message: string; first_seen: string;
  }[];
}

export interface SitesOverview {
  sites: SiteRow[];
  totals: AlarmCounts;
  /** The monitoring's own state. Never added to `totals`. */
  platform: PlatformState;
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
  alarms: AlarmCounts;
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
  /** Where racks live ('white_space') vs plant, switchrooms and the roof
   *  ('facility'). Comes from the simulator's floor plan, not from the room's
   *  name. Null on a room nobody has classified yet. */
  room_class?: string | null;
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
    facility_rooms: number;
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
    /** What the hidden facility rows contribute, so the header reconciles. */
    facility: { rooms: number; total_kw: number | null; cooling_kw: number | null };
  };
  sites: PowerRow[];
  rooms: PowerRow[];
  notes: string[];
}

export interface UtilRow extends EstateRowBase {
  space_pct: number | null;
  space_used_u: number;
  space_total_u: number;
  /** Rack positions the room was drawn with, and how many are standing. */
  designed_racks: number | null;
  built_out_pct: number | null;
  floor_area_m2: number | null;
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
    built_out_pct: number | null; designed_racks: number | null;
    floor_area_m2: number | null; facility_rooms: number;
  };
  sites: UtilRow[];
  rooms: UtilRow[];
  notes: string[];
}

export interface AlarmDrillRow {
  room_id: string;
  room_name: string;
  floor: string | null;
  site_id: string;
  site_code: string;
  site_name: string;
  /** Alarms: what the counter that opened the panel was counting. */
  qty: number;
  /** Informational conditions in the same room and category. Context beside
   *  the alarm count, never added to any total - a room with two cooling
   *  alarms and forty cooling alerts is a different room from one with two
   *  and none. */
  alerts: number;
  /** Distinct devices behind `qty` - the alarms only. */
  devices: number;
  /** The same count over both classes, for a panel that lists both. One
   *  device faulting in two categories is still one device. */
  devices_all: number;
  critical: number;
  major: number;
  by_severity: { critical: number; major: number; minor: number; warning: number };
  by_detection: Record<AlarmDetection, number>;
}

export interface AlarmDrill {
  categories: AlarmCategory[];
  rows: AlarmDrillRow[];
  /** Every open condition in the category, in a room - exactly what the rows
   *  add up to and exactly what the counter that opened the panel counts. */
  total: number;
  /** The actionable part of `total`. */
  alarms: number;
  /** Platform conditions in this category, which belong to no room and are in
   *  NEITHER number above. Reported so the panel can name what it is not
   *  counting and point at the monitoring badge that is. */
  unlocated: number;
  /** The alarm part of `unlocated`. */
  unlocated_alarms: number;
  /** Folded from the rows above, so the facets and the rows can never be two
   *  different instants of the estate. Excludes `unlocated` - those have no
   *  row to face against. */
  by_severity: { critical: number; major: number; minor: number; warning: number };
  by_detection: Record<AlarmDetection, number>;
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
  platformState: () => request<PlatformState>('/sites/platform/state'),

  siteKpi: (datacenterId: string) =>
    request<SiteKpi>(`/sites/${datacenterId}/kpi`),

  devices: (params: Record<string, string | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    const qs = q.toString();
    return request<Page<DeviceSummary>>(`/devices${qs ? `?${qs}` : ''}`);
  },

  // ---- asset workspace (docs/21). Additive; nothing above changes. ----

  assetSummary: () => request<AssetSummary>('/assets/summary'),

  suppliers: () => request<{ items: Supplier[] }>('/suppliers'),
  contracts: (params: Record<string, string | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    const qs = q.toString();
    return request<{ items: SupportContract[]; expiring_days: number }>(
      `/contracts${qs ? `?${qs}` : ''}`);
  },
  contract: (id: string) => request<SupportContract>(`/contracts/${id}`),
  deviceContracts: (deviceId: string) =>
    request<{ items: SupportContract[] }>(`/devices/${deviceId}/contracts`),
  tags: () => request<{ items: Tag[] }>('/tags'),

  lifecycleHistory: (deviceId: string) =>
    request<LifecycleHistory>(`/devices/${deviceId}/lifecycle`),
  lifecycleTransition: (deviceId: string, body: {
    to_state: string; reason?: string; change_ref?: string;
  }) => request<LifecycleEvent>(`/devices/${deviceId}/lifecycle`, {
    method: 'POST', body: JSON.stringify(body),
  }),

  maintenanceWindows: (params: Record<string, string | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    const qs = q.toString();
    return request<{ items: MaintenanceWindow[] }>(
      `/maintenance/windows${qs ? `?${qs}` : ''}`);
  },
  maintenanceWindow: (id: string) =>
    request<MaintenanceWindow>(`/maintenance/windows/${id}`),
  maintenancePreview: (deviceIds: string[]) =>
    request<MaintenancePreview>('/maintenance/windows/preview', {
      method: 'POST', body: JSON.stringify({ device_ids: deviceIds }),
    }),
  maintenanceRecords: (deviceId: string) =>
    request<{ items: MaintenanceRecord[] }>(`/devices/${deviceId}/maintenance`),
  powerChain: (deviceId: string) =>
    request<PowerChain>(`/power/chain/${deviceId}`),
  assetFilterOptions: () => request<AssetFilterOptions>('/assets/filter-options'),

  discoveryCandidates: (params: Record<string, string | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    const qs = q.toString();
    return request<{ items: DiscoveryCandidate[] }>(
      `/discovery/candidates${qs ? `?${qs}` : ''}`);
  },

  /** The same /devices resource, called the way the asset list needs it.
   *
   *  A separate method rather than a wider `devices()` because that one drops
   *  falsy values - which is correct for its callers and wrong here: the
   *  reconciliation queue is `has_serial=false`, and `if (v)` would silently
   *  turn it into "no filter" and return the whole estate looking healthy.
   *  Arrays are emitted as repeated keys, which is what FastAPI reads back as
   *  a list.
   */
  assetDevices: (params: Record<string, string | string[] | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === '') continue;
      if (Array.isArray(v)) v.forEach((one) => { if (one) q.append(k, one); });
      else q.set(k, v);
    }
    const qs = q.toString();
    return request<Page<DeviceSummary>>(`/devices${qs ? `?${qs}` : ''}`);
  },

  device: (id: string) => request<DeviceDetail>(`/devices/${id}`),

  collectors: () => request<CollectorsPage>('/collectors'),

  /** The complete document the page is showing. Whole-document rather than a
   *  patch, so "clear this override and fall back to the collector's file" is
   *  expressible - in a patch an absent key already means "leave it alone". */
  setCollectorConfig: (id: string, config: Record<string, unknown>) =>
    request<{ collector_id: string; version: number; restart_pending: string[] }>(
      `/collectors/${id}/config`,
      { method: 'PUT', body: JSON.stringify({ config }) }),

  pollProfiles: () => request<PollProfilesPage>('/poll-profiles'),

  pollProfileUsage: (id: string) =>
    request<ProfileUsage>(`/poll-profiles/${id}/usage`),

  createPollProfile: (body: PollProfileBody) =>
    request<PollProfileSummary>('/poll-profiles', {
      method: 'POST', body: JSON.stringify(body),
    }),

  /** Returns what moved and how many endpoints followed it. */
  updatePollProfile: (id: string, body: PollProfileBody) =>
    request<{ profile_id: string; changed: Record<string, unknown>;
              endpoints_moved: number }>(`/poll-profiles/${id}`, {
      method: 'PATCH', body: JSON.stringify(body),
    }),

  deviceEndpoints: (id: string) =>
    request<EndpointSummary[]>(`/devices/${id}/endpoints`),

  /** Credentials, poll profiles and the addressing fields each protocol
   *  defines - the same table the server validates against, so the form can
   *  reject a bad unit ID without a round trip. */
  endpointOptions: (params: {
    protocol?: string; q?: string; current?: string;
  } = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    const qs = q.toString();
    return request<EndpointOptions>(
      `/devices/endpoint-options${qs ? `?${qs}` : ''}`);
  },

  /** Only the fields the operator touched. Absent means "leave alone"; null is
   *  a real value - a null port follows the protocol default. */
  updateEndpoint: (deviceId: string, endpointId: string, patch: EndpointPatch) =>
    request<EndpointSummary>(`/devices/${deviceId}/endpoints/${endpointId}`, {
      method: 'PATCH', body: JSON.stringify(patch),
    }),

  deviceState: (id: string) => request<DeviceState>(`/devices/${id}/state`),

  rooms: () => request<{ items: RoomSummary[] }>('/rooms'),

  history: (deviceId: string, metrics: string[], startIso: string, endIso: string) => {
    const q = new URLSearchParams({ start: startIso, end: endIso });
    for (const m of metrics) q.append('metric', m);
    return request<HistoryOut>(`/devices/${deviceId}/history?${q.toString()}`);
  },

  interfaces: (deviceId: string) =>
    request<NetworkInterface[]>(`/devices/${deviceId}/interfaces`),

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

  /** One or more categories in one request: a grouped counter is one question,
   *  and one room must come back as one row. */
  estateAlarms: (categories: string[]) =>
    request<AlarmDrill>('/estate/alarms?'
      + categories.map((c) => `category=${encodeURIComponent(c)}`).join('&')),

  alarmTaxonomy: () => request<AlarmTaxonomy>('/estate/alarm-categories'),

  /** The conditions behind one row of an alarm panel.
   *
   *  Both classes, always. The row above it splits alarms from alerts because
   *  the split is what an operator triages on; once the row is open the
   *  question has changed to "what is actually wrong in this room", and
   *  answering it with half the conditions would make the panel look broken
   *  against its own alert column.
   *
   *  Roots only and open only, the same two filters the roll-up counts with,
   *  so the expansion adds up to the numbers on the row. */
  /** Conditions in one room. `states` selects the lifecycle to show: omitted,
   *  the server answers with the open ones (ACTIVE + ACKNOWLEDGED), which is
   *  what the roll-up counters are counting. */
  roomConditions: (roomId: string, categories: string[], states?: string[]) =>
    request<{ items: Alarm[] }>(
      `/alarms?limit=500&room=${encodeURIComponent(roomId)}`
      + categories.map((c) => `&category=${encodeURIComponent(c)}`).join('')
      + (states ?? []).map((s) => `&state=${encodeURIComponent(s)}`).join('')),

  /** Who this instance belongs to. Unauthenticated on purpose: the shell needs
   *  a name before anybody has signed in. */
  instance: () => request<Instance>('/instance'),

  roomKpi: (roomId: string) => request<RoomKpi>(`/estate/rooms/${roomId}/kpi`),

  collectorHealth: () => request<CollectorHealth>('/collector/health'),

  wsTicket: () =>
    request<{ ticket: string; expires_in: number }>('/ws/ticket', {
      method: 'POST', body: '{}',
    }),
};
