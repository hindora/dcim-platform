# 12 — React Frontend Specification

React 18 + TypeScript + Vite. REST for state on mount and for CRUD; WebSocket for
deltas. Never a polling loop.

---

## 1. Stack

| Concern | Choice | Reason |
|---|---|---|
| Routing | react-router v6 | nested routes match the navigation tree |
| Server state | TanStack Query | caching, invalidation, request dedup |
| Live state | zustand store fed by one WS client | the WS reducer is the only mutator |
| Charts | uPlot (time series), ECharts (heatmaps, sankey) | Recharts stalls above ~2k points |
| Topology | Cytoscape.js, or react-flow for small graphs | Cytoscape handles 2k nodes with layouts |
| Tables | TanStack Table + virtualisation | 664-row device tables must virtualise |
| Styling | CSS variables + a token file | no raw hex at call sites |
| Forms | react-hook-form + zod | zod schemas generated from the OpenAPI where possible |
| API client | generated from `contracts/openapi/dcim-v1.yaml` | drift becomes a compile error |

---

## 2. Navigation

Matches the requested structure, with two additions justified below.

```
Dashboard

Infrastructure
  Datacenters · Rooms · Rows · Racks · Devices · Floor Plan

IT Infrastructure
  Servers · Switches · Routers · Firewalls · Load Balancers

Power
  Overview · Grid & Utility · Generators · Switchgear · ATS · UPS
  Floor PDU · Rack PDU · RPP · Power Chain

Cooling
  Overview · Chillers · Cooling Towers · Pumps · CRAH · CRAC · CDU · Valves · Plant Loops

Environment
  Overview · Temperature · Humidity · Airflow · Dew Point · Heat Map

Topology
  Network · Power · Cooling · Physical · Management

Monitoring
  Alarms · Events · Trends · Collector Health          ← added

Analytics
  PUE · Capacity · Power Analytics · Thermal Analytics · Forecast

Simulator
  Device Lifecycle · Fault Injection · Load Simulation

Administration                                          ← added
  Alarm Rules · Discovery · Credentials · Users · Metric Registry
```

**Collector Health** is added because a DCIM whose collection plane is degraded
shows stale numbers with no indication that they are stale. Operators need one
page that says "col-1 has been failing to reach 40 endpoints for 12 minutes".

**Administration** is added because alarm rules, credentials and discovery
policies have to live somewhere, and burying them in a modal makes them
un-auditable.

---

## 3. Key views

### 3.1 Dashboard

Grid of tiles from `GET /dashboard/summary`, live-patched from the `dashboard`
topic:

| Tile | Content |
|---|---|
| Devices | total / online / offline / degraded, donut |
| Alarms | critical / major / warning counts, click-through, suppressed-symptom count shown separately |
| IT Power | kW + 24 h sparkline |
| Cooling Load | kW + sparkline |
| PUE | value, method badge (energy/power), measurement level badge |
| Temperature | avg + max inlet, hot-spot count |
| Humidity | avg, band indicator |
| Plant Health | chillers running / standby, CHWS temp |
| Collectors | per-collector status chips |
| Power Trend | 24 h stacked: IT vs facility |
| Cooling Trend | 24 h: load vs plant capacity |
| Recent Alarms | latest 10, roots only |

Rules that keep it honest: every tile shows its `as_of` age and greys out past
2× its expected update interval. A dashboard that silently shows five-minute-old
numbers during a collector outage is worse than one that shows nothing.

### 3.2 Device detail

Tabs: **Overview · Metrics · Alarms · Events · Relationships · Endpoints · Config**

- *Overview* — identity, location (breadcrumb DC → Room → Row → Rack → U, each a
  link), model/vendor/serial, communication status per endpoint with last-seen
  age, current hot metrics.
- *Metrics* — every metric the device reports with its current value; select up
  to 6 to chart together; range picker (1 h / 6 h / 24 h / 7 d / 30 d / custom);
  the chart labels which aggregate it is reading (`raw`, `1m`, `1h`).
- *Alarms* — active and historical, with the ack/clear actions inline.
- *Relationships* — layer tabs; a small graph plus a list of upstream/downstream
  devices.
- *Endpoints* — protocol, address, credential name (never the secret), poll
  interval, health counters, and the **Test** button.
- *Config* — editable inventory fields, gated on role, with `If-Match`.

### 3.3 Rack view

An actual elevation, not a table.

```
┌─ Rack R2-01 ────── 8.4 / 12.0 kW (70%) ── max inlet 24.1 °C ── 6U free ─┐
│ U42 ▐████ SRV01-DC1-HA-R2-01     ● ONLINE   812 W   23.4 °C            │
│ U41 ▐████ SRV02-DC1-HA-R2-01     ● ONLINE   798 W   23.6 °C            │
│ U39 ▐████ SRV03-DC1-HA-R2-01     ▲ WARNING  845 W   28.9 °C   (2U)     │
│ ...                                                                    │
│ U18 ░░░░ (free, 6U block)                                             │
│ U12 ▐████ LF01-DC1-HA-R2-01      ● ONLINE                             │
│ U02 ▐████ PDUA-DC1-HA-R2-01      ● ONLINE   4.1 kW  68%               │
└────────────────────────────────────────────────────────────────────────┘
```

- One request: `GET /racks/{id}/elevation`.
- Device rows are colour-coded by `max_severity`, with an explicit status glyph
  as well — colour alone fails for colour-blind operators and in a printed
  runbook.
- Front/rear toggle; multi-U devices render at their true height.
- Hovering shows a metric popover; clicking opens the device.
- A "power" overlay tints each device by draw, and a "thermal" overlay by inlet
  temperature. These two overlays are how a rack view earns its place over a
  list.

### 3.4 Topology

- Layer switcher; scope picker (DC / room / rack / device + depth).
- Layouts: `dagre` hierarchical for power and cooling (they are trees), `cola`
  or `fcose` for network.
- Node colour = severity; node badge = device type; edge style = layer, with
  A-side and B-side power paths drawn distinctly (solid vs dashed) so a lost
  redundancy is visible at a glance.
- Live: `topology:{layer}` updates node colour and edge `oper_state` in place —
  never re-layout on an update, which is disorienting.
- Click a node → side panel with current state and a link to the device page.
- Above 2,000 nodes the UI requires a narrower scope rather than attempting a
  layout that will not finish.

### 3.5 Alarms

- Default filter: `state=ACTIVE`, roots only, sorted by severity then last_seen.
- A root with symptoms renders as one row with a `+7 symptoms` chip that expands
  inline.
- Bulk select → acknowledge with a note.
- Live-updating via the `alarms` topic, with a visible "3 new alarms" pill rather
  than reordering the list under the operator's cursor — reordering a list
  someone is clicking is a real operational hazard.
- Row click → drawer with the alarm's history, the device's recent telemetry
  around `first_seen`, and the related events.

### 3.6 Floor plan

- Room outline with racks at their `floor_x`/`floor_y`.
- Overlays: temperature heat map (interpolated from rack inlet sensors), power
  density, alarm status, capacity headroom.
- Cold/hot aisle shading from the row metadata.
- CRAH and CDU positions shown, with the racks each serves highlighted on hover —
  this is the view that makes a cooling dependency obvious in a way no list does.

### 3.7 Collector health

Per collector: status, uptime, version, endpoints owned/online/failing, poll rate,
publish queue depth, drop counters, and a table of the endpoints with the most
consecutive failures. This page is the first stop when numbers look wrong.

---

## 4. State management

```ts
// One WS client, one reducer, one store.
interface LiveState {
  deviceState: Record<string, DeviceLiveState>;   // status, health, hot metrics + ts
  alarms: Record<string, Alarm>;
  dashboard: DashboardSummary | null;
  collectors: Record<string, CollectorStatus>;
  connection: "connecting" | "open" | "reconnecting" | "closed";
  lastFrameAt: number;
}
```

Rules:

1. REST populates the store on mount; WS applies deltas afterwards.
2. Every live value carries its timestamp; an out-of-order frame is dropped.
3. On reconnect, invalidate the relevant TanStack Query keys and refetch — do not
   assume the store survived the gap intact.
4. Components read derived selectors, never raw frames.
5. `connection !== "open"` renders a persistent banner. Silence is not an
   acceptable indication that the live feed is dead.

---

## 5. Units and formatting

One module, generated from the metric registry:

```ts
formatMetric("cpu_temperature", 67.5)   // "67.5 °C"
formatMetric("power_draw", 812)         // "812 W"
formatMetric("power_draw", 812_000)     // "812 kW"   — auto-scaled
formatMetric("if_in_bps", 1_250_000)    // "1.25 Mbps"
```

No component ever hardcodes a unit string. When a metric's unit changes (which
means a new metric key, per the registry rules), nothing in the UI needs editing.

Temperature display honours a user preference (°C/°F) at the **formatter** only —
the store and the API are always °C.

---

## 6. Performance

- Virtualise every list over 100 rows.
- Charts receive pre-decimated data from the API (the `interval` parameter), so
  the browser never decimates 100k points.
- `React.memo` on rack-position and topology-node components; they re-render on
  every live tick otherwise.
- Code-split by feature route; the topology and chart libraries are the two
  largest bundles and must not be in the initial chunk.
- Target: dashboard interactive in < 1.5 s on a cold load, rack view < 500 ms.

---

## 7. Accessibility and operational fitness

- Status is never conveyed by colour alone — always a glyph or text as well.
- Keyboard navigable tables and drawers; the alarm list must be usable without a
  mouse.
- Dark theme is the default (control rooms are dark) with a light theme
  available; both defined as token sets, not as scattered overrides.
- Time zone: display in the datacenter's local time with a UTC tooltip. Storing
  UTC and displaying UTC is technically correct and operationally annoying when
  the person reading it is standing in the hall.
