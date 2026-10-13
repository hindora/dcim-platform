# 27 — 3D Digital Twin Plan

A browser-based 3D twin of the estate (site → building → floor → room → row →
rack → device) that carries live thermal, power, space and alarm state, with
time playback and a CRAH-failure what-if.

Written 2026-10-07 against the working tree at `92b59da` (dcim-platform) and
`744dad5` (simulator, branch `Faberwork-release-Datacenter_Network_Simulator_v6.1`).
References: EkkoSense EkkoSoft Critical and Huawei NetEco6000 (requested), plus
Sunbird dcTrack, Schneider EcoStruxure IT, Siemens/Vigilent, Cadence Reality DC
and NVIDIA Omniverse for contrast. Nothing in this document is built yet.

---

## 0. The verdict before the design

1. **"Digital twin" means two different products in this market, and we should
   build the operations one.** Design twins (Cadence Reality DC / 6SigmaDCX,
   Schneider IT Advisor CFD, EkkoSIM, NVIDIA Omniverse) run CFD on a calibrated
   model for planning and what-if. Operations twins (EkkoSoft Critical,
   NetEco6000, Sunbird dcTrack, Nlyte, Hyperview) put live sensor values on a 3D
   model of the room. **Nobody runs full CFD on live data.** EkkoSense itself
   sells CFD separately (EkkoSIM); its live view is rack-level sensor colouring
   refreshed every 5 minutes. We build the operations twin, and do what-if with
   a fitted influence model, not CFD (§3 D5).

2. **A 3D heat map today would show a flat field, because the simulator's air
   is uniform.** Every rack in a hall shares one supply temperature
   (`device_state_store._room_supply_temp`); inlet varies only by height in the
   rack (0–3 K ramp) and ±0.2 K noise; a dead CRAH warms the whole room equally;
   humidity is a random walk uncoupled from temperature. The simulator's own
   comment (`device_state_store.py:58-66`) admits the local effect of a failed
   unit "is a spatial effect the model has no geometry for". Rendering that in
   3D would be a convincing picture of nothing. **The simulator needs a spatial
   thermal model (Phase S2) before the thermal layer is worth shipping.** Every
   other layer (space, power, alarms, paths) works on today's data.

3. **Positions do not come from devices.** No protocol (SNMP, Redfish, BACnet,
   Modbus, gNMI) reports where a rack stands. In production the geometry comes
   from CAD/BIM, a room builder in the DCIM, or a delivery team (NetEco's 3D
   View-Lite "automatically generates 3D view based on 2D layout"; EkkoSense
   builds rooms by hand, no CAD import). Our importer reading coordinates from
   the simulator stands in for that CAD/asset import. It must stay a separate,
   overridable source — never mixed into telemetry.

4. **Most of the platform side already exists.** Racks carry `floor_x/floor_y`
   and `facing`, devices carry `u_start/u_height`, the power chain, impact and
   trace APIs exist, `device_state` holds the latest value per device, and
   `/connectivity` already shows how to lazy-load one heavy route. The gaps are
   specific: the importer drops aisles, rack footprint, mounting and plant
   positions; there is no building/level model; there is no room-scoped live
   topic.

---

## 1. What exists today (verified)

### 1.1 dcim-platform

| Area | What exists | Where |
|---|---|---|
| Room | `floor` (text), `room_class`, `width_m`, `depth_m`, `attributes.containment/designed_rows/racks_per_row` | `models/inventory.py:51-81`, migration 0017 |
| Row | `ordinal`, `cold_aisle`, `hot_aisle` | `models/inventory.py:88-101` |
| Rack | `ordinal`, `u_height`=42 (hard-coded), `facing` N/S, `floor_x/floor_y` (centre, room metres), `rated_power_kw`, `rated_cool_kw` | `models/inventory.py:108-124` |
| Device | `room_id`, `rack_id`, `u_start`, `u_height` (guessed from model name), `facing` (always NULL), `floor_x/floor_y` | `models/inventory.py:192-263`, `importer/simulator.py:1230-1236` |
| 2D floor plan | SVG in metres, 0.6×1.2 m racks, aisle bands, thermal/power/alarm overlays, 20 s refresh | `features/floorplan/FloorPlan.tsx`, `GET /rooms/{id}/floorplan` |
| Rack elevation | status/power/thermal overlays, zero-U list | `features/racks/RackElevation.tsx`, `/racks/{id}/elevation` |
| Latest values | `device_state` typed columns + `metrics` JSONB, `thermal_racks_now` (10 min horizon) | `models/state.py:55-88`, `repositories/estate.py:1026` |
| History | `/devices/{id}/history`, estate thermal trends, `telemetry_5m` / `telemetry_1h` caggs | `api/v1/devices.py:193`, `api/v1/estate.py:245-326` |
| Live push | `/ws` ticketed, topics `device:{id}`, `dashboard`, `alarms*`, `events`; **50 topics per session, no room topic** | `api/v1/ws.py`, `ingest/fanout.py:58-104`, `core/config.py:100` |
| Alarms → place | `COALESCE(rack_row.room_id, device.room_id)`; `device_state.max_severity` roll-up | `repositories/alarms.py:330-332, 560-590` |
| Power / cooling paths | `connection.layer` power/cooling/…, A/B sides, `/power/chain`, `/topology/trace`, `/topology/impact` | `models/inventory.py:333-378`, `services/trace.py`, `services/impact.py` |
| Frontend | React 18.3, router 6, react-query 5, @xyflow/react; no three/d3; CSS tokens in `index.css`; `/connectivity` is the only lazy route | `frontend/package.json`, `App.tsx:55-64` |

### 1.2 Defects found while mapping (fix in Phase 0)

- `services/floorplan.py:6` says room `width_m/depth_m` are always null, and
  `services/devices.py:room_floorplan` (198-233) still derives the room outline
  from the rack bounding box — but the importer now fills both. The plan draws a
  room smaller than the real one.
- CRAH/UPS/etc. positions are imported (`floor_x/floor_y`) but
  `repositories/racks.py:floorplan_equipment` never selects them, so the 2D plan
  lists CRAHs under the drawing instead of on it.
- The importer keeps only the *count* of `floorplan.rooms[].rows` and drops
  `aisles[]`, `rack_footprint`, `rack_pitch`, `row_pitch`, `aisle_width`,
  `origin`, device `mounting`, `airflow`, `sensor_slot` (`importer/simulator.py:363-432`).

### 1.3 Simulator

- Grid geometry is sound and centralised: `core/hall_geometry.py`
  (`rack_x = 0.3 + 0.6·(n-1)`, `row_y = 1.8 + 2.4·(i-1)`, odd rows face S).
- `GET /api/topology/export` carries per-device placement and the `floorplan`
  block, but **not** `u_height`, rack objects, or `mounting` (dropped by
  `Device.from_dict`, `device_manager.py:2353-2367`).
- `GET /api/floorplan` (`tools/export_dcim_floorplan.py`, schema
  `dcim-floorplan/1.0`) is already normalised: rack ids, `u_height`,
  `feed_a/feed_b`. The importer does not use it.
- **No building model**: rooms have no origin relative to each other; "floor" is
  a label (`'G'`, `'1'`, `'2'`, `'Roof'`). All Central Plant gear shares one
  slot (0.3, 0.6).
- Data defects: five DC1 Hall B sensors on floor `'1'` while their racks are on
  `'2'`; `SEN1-DC1-HA-R2-03/-05` (and HB twins) carry `floor_x 0.3` but belong to
  racks 3 and 5.
- The simulator already has a 3D viewer: `webui/public/floorplan_viewer.html`
  (generated by `tools/build_floorplan_viewer.py`, three.js r128 from CDN,
  procedural CRAH/UPS/chiller meshes, IDW floor heat plane). It is the
  simulator's ground-truth view, not the product. We do not extend it — but its
  procedural mesh shapes are a useful reference.
- Thermal model: see §0.2.

---

## 2. How the reference products do it

Marked **[V]** verified from a cited source, **[I]** inference.

### 2.1 EkkoSense EkkoSoft Critical

- **[V]** "Virtual 3D model of the data center"; rooms built by hand in a "3D
  Rapid Room Builder" or by EkkoSense's remote build service; **no CAD import**.
  Estate → room → room info drill. Release 9.4 added 3D objects for DLC racks,
  CDUs, immersion tanks, external plant and power gear.
- **[V]** Live view is sensor analysis, not simulation; data every **5 min**.
  "Temperature gradients across rack inlets and ACUs" — colour on rack faces and
  cooling units, **[I]** not a room-wide cloud.
- **[V]** **Zones of Influence**: an ML clustering that maps racks to the cooling
  unit that serves them, continuously re-fitted; shown as a filter layer.
- **[V]** Sensors: wireless T/RH, "at least one per rack", more for gradients;
  EkkoAir inside the CRAH measures supply/return + fan power via CTs → cooling
  duty kW. Integration via Modbus, SNMP, oBIX (BACnet not named).
- **[V]** Cooling Advisor recommends setpoints, fan speeds, grille moves —
  advisory only, with approval and rollback. What-if lives in EkkoSIM.
- **[V]** Power layers: 3-phase balance, busbar capacity in 5 colour bands.
- **[V]** "Gaming-engine based", SaaS on AWS, browser. Engine unnamed.
- Sources: ekkosense.com/faqs, /ekkosoft-critical/digital-twin,
  /resources/tech-tips/taking-advantage-of-zones-of-influence-couldnt-be-easier,
  /ekkosoft-critical/cooling-advisor-data-center-cooling-best-practice.

### 2.2 Huawei NetEco6000

- **[V]** 3D is a paid option, three exclusive tiers: **View-Lite** (building,
  floor, room, module, cabinet; "built-in 3D engine, 2D/3D one-click switching",
  auto-generated from the 2D floor plan), **View-Pro** (campus, scenery, IT
  device panels and connections), **View-BIM** (imports BIM, cables, trays,
  pipes, model cutting, measurement).
- **[V]** **Three-layer temperature map**: Top / Middle / Bottom cloud, chosen by
  the operator, adjustable colour range, top-5 hot and cold spots, from
  door-mounted cabinet sensors. Interpolation method unpublished.
- **[V]** Auto-generated **power link** and **cooling link** diagrams with
  click-through from alarms, and **fault simulation / pre-rehearsal** of impact
  range. SPCN capacity (space, power, cooling, network) in 3D, best-location
  finder, tenant view.
- **[V]** iCooling@AI (DNN + genetic search, 5 min samples) is a separate
  licence, not drawn in the twin. U-level asset detection via RFID strips.
- Sources: NetEco6000 datasheet (digitalpower.huawei.com/attachments/data-center-facility/69d95f99d691414f8b71c6aaa83097aa.pdf),
  iManager NetEco brochure, support.huawei.com EDOC1100195398 (temperature map).

### 2.3 What the rest of the market adds

- **Sunbird dcTrack [V]**: thermal and pressure maps on several planes with
  **time-lapse playback** ("weather radar"). No CFD.
- **Siemens WSCO / Schneider Cooling Optimize (Vigilent) [V]**: machine-learned
  influence map of which cooling unit affects which sensor, from ~2 thermistors
  on every 5th rack; a failed unit shows as no influence. This is the realistic
  source for live what-if.
- **Schneider IT Advisor [V]**: Capture Index + potential-flow model; design-time.
- **Hyperview [V]**: 2D/3D toggle with rack contents, underfloor, ceiling assets.

### 2.4 Borrow / reject

| Borrow | From | Why |
|---|---|---|
| 2D/3D toggle on the same room | NetEco, Hyperview | Operators work in 2D; 3D is for context and for stakeholders |
| Three-height inlet layers (bottom/mid/top) | NetEco | Matches ASHRAE sensor placement; honest about where data exists |
| Zones of influence as a layer | EkkoSense, Vigilent | Answers "which racks does CRAH-3 cool?" |
| Power/cooling path trace + impact pre-rehearsal | NetEco | We already have `/topology/impact`; 3D is a natural surface |
| Time-lapse playback | Sunbird | We have caggs; incidents are understood by replay |
| Space/power/cooling capacity colouring | NetEco SPCN, EkkoSense busbar bands | Existing CAPACITY data |

| Reject | Why |
|---|---|
| Live CFD | Nobody does it; we have no calibrated model |
| BIM import (web-ifc) | Real DC IFCs rarely carry rack detail; defer |
| Robot tours, campus scenery, grass | Marketing, no operator value |
| Composite score (EkkoScore) | Already decided against (doc 21 / home redesign) |
| Photoreal vendor model library | Racks and servers are boxes; procedural geometry is faster and smaller |
| EkkoSense look | Standing rule: borrow information architecture, never the theme |

---

## 3. Design decisions

**D1 — Build it in dcim-platform, as a new lazy route `/twin`.** The product is
the DCIM; the simulator's viewer stays the ground truth. Same pattern as
`/connectivity` (`App.tsx:55-64`): three.js never enters the main chunk.

**D2 — react-three-fiber + drei on three.js.** Fits React 18 + Vite; three core
~170 KB gz vs Babylon ~1.9 MB. Rendering rules:
- Raw `THREE.InstancedMesh`, one per geometry kind (rack frame, 1U, 2U, 4U,
  zero-U PDU, sensor puck); colour via `setColorAt`. A 400-device room is <20
  draw calls; drei `<Instances>` is too slow at this count.
- Picking by `instanceId` → index→device-id table.
- Procedural geometry only (boxes + a few CRAH/UPS/chiller shapes ported from
  the simulator viewer). No glTF pipeline in v1.
- LOD: estate/building level draws rooms and racks only; devices appear for the
  room in focus; labels (drei `<Text>`, SDF) only for selection and hover.
- Heat planes are a `DataTexture` (one texel per 0.3 m) on a plane with a colour
  ramp in the fragment shader; updates rewrite the texture, never the geometry.
- Colours are read from the existing CSS tokens at runtime
  (`getComputedStyle`) so light/dark theme and `--ok/--warn/--major/--critical`
  stay single-sourced (standing rule: no raw hex at call sites).

**D3 — One coordinate system, stored, not derived.** Room-local metres with the
room corner at (0,0), +x along the row, +y across rows, +z up from the raised
floor. Rooms carry a placement in their building level (origin, rotation, level
elevation). Racks: centre, footprint, facing. Devices: rack + U, or room x/y
(+ mount) for floor-standing gear. Sensors: rack + side (front/rear) + height
in metres. This mirrors Brick/Haystack relationship structure
(space → equipment → point) in Postgres tables; no RDF store.

**D4 — Geometry is an import, not telemetry, and is editable.** Importer
writes geometry with `source = 'import'`; a later room-builder edit writes
`source = 'manual'` and the importer stops overwriting that field. v1 ships
import-only; the editor is Phase 6. This is how every real DCIM works (§0.3).

**D5 — Thermal field: per-plane interpolation on the backend, never across
containment.** For each room and each of three heights (bottom ≈ 0.5 m, mid
≈ 1.2 m, top ≈ 1.8 m, matching ASHRAE inlet placement):
- cold-aisle surface from inlet sensors only; hot-aisle surface from
  exhaust/rear sensors only. Containment walls and rack rows are barriers. A
  room-wide IDW blends hot exhaust into cold inlet — the most common unrealistic
  shortcut — and is forbidden.
- method: IDW within an aisle in v1; ordinary kriging in v2 because its variance
  lets the UI **fade areas no sensor covers** instead of painting confident
  colour there.
- sensor source follows the existing probe-first rule (rack front probes, else
  server BMC inlet) so the twin and the THERMAL page cannot disagree.
- the grid is computed in Python (numpy) per room and served as a compact
  payload; the browser only draws it.

**D6 — Indices the twin shows are the ones computable from our sensors.**

| Index | Formula | Inputs | Shown on |
|---|---|---|---|
| RCI_HI / RCI_LO (Herrlin) | 1 − Σ over-temp / ((T_max,all − T_max,rec)·n), ×100 % | rack inlets, ASHRAE A1 18/27 rec, 15/32 allow | room |
| RTI | (T_return − T_supply) / ΔT_equip ×100 % (<100 bypass, >100 recirc) | CRAH supply/return, airflow-weighted rack ΔT | room, CRAH |
| SHI / RHI | (T_in − T_ref)/(T_out − T_ref); RHI = 1 − SHI | rack inlet/exhaust, supply of nearest/influencing CRAH | rack |
| Capture Index | needs airflow tracing | — | **not shown**; not measurable from temperatures |

Formulas to be checked against Herrlin/ANCIS before shipping (the research
recall is unverified for RCI and RTI exact form).

**D7 — Live updates: poll first, room topic second.** Sensors move every
~70–270 s and the 2D plan already refreshes at 20 s, so v1 polls a single
`/twin/rooms/{id}/state` snapshot every 15 s. Phase 3 adds a `room:{id}` WS
topic that pushes batched `[device_idx, value]` deltas (per-device topics are
impossible under the 50-topic cap).

**D8 — Playback reads frames from the caggs.** `/twin/rooms/{id}/frame?t=`
returns the same shape as the live snapshot, from `telemetry_5m` (≤ 2 days) or
`telemetry_1h`; the scrubber prefetches neighbours. Same source-selection rules
as the thermal trend (§ memory: 1h cagg closes an hour late; raw tail for the
newest 2 h).

**D9 — What-if = influence matrix, not CFD.** Fit, per room, how much each
CRAH's supply/state moves each rack inlet (regression over history, with CRAH
trips as natural experiments). "What if CRAH-3 fails" = remove its column,
re-normalise, predict inlets, colour racks, list the ones that leave the ASHRAE
band. Label the result "model estimate" with the fit's error. Only meaningful
once Phase S2 gives the simulator real spatial coupling — otherwise the fit
finds nothing, correctly.

---

## 4. Data model (platform migrations)

Next migration numbers are assigned at implementation time.

### 4.1 Building / level
- New table `building_level`: `id`, `datacenter_id`, `name` (`G`, `1`, `2`,
  `Roof`), `ordinal`, `elevation_m`, `slab_height_m`, `outline` (JSONB polygon,
  nullable — fallback is the bounding box of its rooms).
- `room`: add `level_id`, `origin_x_m`, `origin_y_m`, `rotation_deg`,
  `ceiling_height_m`, `raised_floor_m`, `geometry_source` (`import|manual`).
  Promote `containment` from `attributes` to a column.

### 4.2 Aisles
- New table `aisle`: `room_id`, `name` (CA1/HA1), `kind` cold|hot,
  `y_m`, `width_m`, `between_rows` int[], `contained` bool. Today's aisles are
  derived from rack facing in `services/floorplan.py:76-120`; store them and keep
  the derivation only as a fallback.

### 4.3 Rack / device / sensor
- `rack`: add `width_m` (0.6), `depth_m` (1.2), `height_m` (2.0 for 42U),
  `rotation_deg` (derived from `facing` when null), `geometry_source`. Stop
  hard-coding `u_height=42`.
- `device`: add `mount` (`rack_front|rack_rear|zero_u|floor|wall|ceiling`),
  `rotation_deg`, `footprint_w_m`, `footprint_d_m`, `height_m` for floor-standing
  plant; take `u_height` from the export instead of guessing from the model name.
- Sensors (devices with `device_type='sensor'` and PDU-hosted probes): add
  `side` (`front|rear`) and `height_m`. A PDU probe slot gets its own row in a
  small `sensor_point` table (`device_id`, `slot`, `rack_id`, `side`,
  `height_m`), because one PDU carries several probes at different heights.

### 4.4 Twin cache
- No new hypertable. The interpolated grid is computed on request and cached in
  Redis per (room, plane, minute) with a short TTL. Playback frames are computed
  from caggs on demand.

---

## 5. Backend

### 5.1 Importer (`importer/simulator.py`)
- Read `floorplan.rooms[].aisles[]`, globals (`rack_footprint`, `rack_pitch`,
  `row_pitch`, `aisle_width`), and the new `building` block (Phase S1).
- Take `u_height`, `mount`, `sensor_slot`/height from the export (Phase S1 adds
  them). Delete the model-name U-height guess (`simulator.py:1230-1236`) once the
  export carries the real value.
- Respect `geometry_source='manual'`.

### 5.2 New service `services/twin.py` + router `api/v1/twin.py`

| Endpoint | Returns |
|---|---|
| `GET /twin/sites/{id}/scene` | levels, rooms (outline, origin, rotation, class), per-room rack count and max severity — the building view |
| `GET /twin/rooms/{id}/scene` | static geometry: room, aisles, racks, devices (rack+U or x/y+mount), sensors, CRAH/CDU/PDU/RPP; stable `idx` per device for instanced picking. ETag'd, changes only on import/edit |
| `GET /twin/rooms/{id}/state?layer=` | live values keyed by `idx`: severity, power/rated, free U, inlet (probe-first), exhaust, CRAH supply/return/state; one query per layer, reusing `thermal_racks_now`, `floorplan` rack rows and `device_state` |
| `GET /twin/rooms/{id}/field?plane=bottom|mid|top&side=cold|hot&t=` | interpolated grid (Float32 → base64), min/max, per-cell confidence, sensor list used |
| `GET /twin/rooms/{id}/indices?t=` | RCI_HI/LO, RTI per CRAH and room, SHI/RHI per rack |
| `GET /twin/rooms/{id}/frame?t=` | state + field for playback, from caggs |
| `POST /twin/rooms/{id}/whatif` | `{fail: [crah_id…]}` → predicted inlets per rack, racks leaving band, model error (Phase 5) |

Path trace and impact reuse `/power/chain`, `/topology/trace`,
`/topology/impact` unchanged; the frontend maps returned device ids onto scene
`idx`.

### 5.3 Fix the 2D plan in the same pass
Use stored `width_m/depth_m` for the outline and draw floor-standing equipment
from `floor_x/floor_y` (§1.2). The 2D plan and the 3D scene must read the same
`/twin/rooms/{id}/scene` geometry so they cannot disagree.

---

## 6. Simulator changes (Phase S)

Per project rule, the simulator models the physics; the DCIM only observes it.

### S1 — Geometry export (small)
- Add a `building` block to each DC's floorplan: levels (`name`, `elevation_m`),
  per-room `origin`, `rotation_deg`, `level`. Lay the curated rooms out once with
  a tool script (`tools/layout_buildings.py`), same style as `enlarge_halls.py`.
- Keep `mounting` through `Device.from_dict` (add the field) and emit
  `u_height` (from `rack_capacity.device_u_height`) in `/api/topology/export`.
- Give Central Plant gear real distinct coordinates and footprints (today all
  share 0.3, 0.6); towers on the Roof level.
- Fix the data defects in §1.3 (HB sensors on floor `'1'`; sensors at
  `floor_x 0.3` in racks 3/5).
- `provision_rack/provision_hall` must emit the same fields for fleet halls.

**S1 status — BUILT 2026-10-07 (simulator, uncommitted).** `core/equipment_geometry.py`
(footprint catalog, effective mount, facing, centre height, `complete_building`);
`Device.mounting` / `Device.rotation_deg`; every exported device now carries
`mount`, `u_height`, `footprint_m`, `facing_deg`, `mount_height_m`;
`/api/topology/export` and `/api/floorplan` (schema `dcim-floorplan/1.1`) carry
`floorplan.buildings[dc]` (levels G 0 m / 1 5 m / 2 10 m / Roof 15 m, outline
27.4 × 15 m) and per-room `level` + `origin`. `tools/layout_buildings.py` resized
the plant rooms to datasheet footprints (e.g. Generator Room 1.8 × 4.8 → 10 ×
12.6 m), and fixed 22 probe defects. In `/api/floorplan`, floor-standing gear no
longer sits under a fake rack. **Still open:** each hall's 7 × 1.75 m CRAHs on 1.0 m
centres overlap. That waits for S2, which re-grids the halls.

### S2 — Spatial thermal model (medium–large; the critical path)

**Full spec (2026-10-08):** `Datacenter_Network_Simulator/docs/S2_SPATIAL_THERMAL_MODEL.md`.
It supersedes the sketch below. It adds: local starvation in place of the
room-wide trip penalty; containment-aware recirculation (the halls are
cold-aisle contained, so today's 0–3 K vertical rise is the uncontained figure);
per-CRAH return; psychrometric humidity; decision **D-1** on where the seven
CRAHs per hall stand (they can't fit on one 8.4 m wall); and a `shadow` flag
that answers Q1 with a day of measured before/after.
How it works in real halls: each CRAH dominates the racks nearest its
discharge; inlet temperature rises with distance from the units, at row ends
(wrap-around recirculation) and at the top of the rack (over-the-top
recirculation when uncontained); a failed unit produces a local hot zone, not a
uniform rise; RH falls where air is warmer because absolute moisture is roughly
uniform in a room.

Proposed model (replaces the room-uniform supply in
`device_state_store._room_supply_temp` with a per-rack value):
1. **Influence weights.** For rack *i* and CRAH *j*:
   `w_ij = exp(-d_ij / L)` with `d_ij` from the rack's cold-aisle face to the
   unit's discharge point and `L ≈ 4–6 m`, normalised over all units.
2. **Per-rack supply.** `T_sup,i = Σ_j w_ij · T_sup,j` over *running* units +
   `(Σ_j∈failed w_ij) · unmet_K_local`, so a trip warms its own neighbourhood
   most. Keep the room-level unmet-capacity term for total shortfall.
3. **Recirculation.** `+ r_i · (T_exhaust,neighbours − T_sup,i)` where `r_i` is
   higher at row ends and top U, scaled by rack power density, and much lower
   with cold-aisle containment (leakage only).
4. **Per-CRAH return.** Weighted mean of the exhaust of the racks it serves
   (transpose of `w`), replacing one shared room return.
5. **Humidity.** Hold a room humidity ratio (g/kg) as the random walk; derive RH
   per sensor from its temperature via psychrometrics. Dew point then stays
   physically consistent.
6. **Sensors.** Add top/mid/bottom inlet probes on row-end racks and every
   third rack (the Vigilent-style density, not one per U); give the PDU probe a
   height term.

Verification (per the cooling-model rules in memory): extend the two-DC
fixture (`conftest.build_two_dc_plant`) with a geometry test (trip one CRAH →
its nearest racks rise most, far racks least, other DC unaffected); run
`tools/live_campaign_exhaustion.py` after; respect "plant walk beats the live
value" for any new live-driven point. **Datasets must be regenerated**: SNMP
datasets are file-backed and fingerprinted over topology, so a generator change
does not travel on restart (Stop → Regenerate Datasets → Start).

---

## 7. Frontend

### 7.1 Information architecture
- New top-nav entry **TWIN** (or a 2D/3D toggle on the existing floor plan —
  open question Q2).
- Levels of the scene: **Site** (building massing, levels stacked, rooms as
  blocks coloured by worst severity / thermal compliance / power %) → **Level**
  (rooms as floor plans) → **Room** (racks, plant, aisles, containment) →
  **Rack** focus (devices by U, front/rear) → click → existing rack elevation or
  device page.
- URL carries state (`?site=&room=&layer=&plane=&t=`), same convention as the
  estate drill (`useEstateTable`), so views are shareable and back works.

### 7.2 Layers (one active colour layer at a time + overlays)

| Layer | Colours | Data |
|---|---|---|
| Alarms | rack/device by `max_severity`, pulsing only for CRITICAL | `device_state` |
| Thermal – rack inlet | rack front face by max inlet, ASHRAE bands (warn 27 / crit 32, aligned with THERMAL page) | probe-first rule |
| Thermal – field | bottom/mid/top plane, cold or hot side, fade where low confidence | `/field` |
| Power | rack load % of `rated_power_kw`, EkkoSense-style bands (≥80 % amber, ≥90 % red) | floorplan rack rows |
| Space | free U per rack | elevation data |
| Cooling zones | racks tinted by dominant CRAH (Phase 5) | influence matrix |
| Overlays | power path (A red / B blue), cooling path, impact set of a selected device | existing trace/impact APIs |

Side panel = existing components (room KPIs from RoomDrawer, `RoomCooling`
table, AlarmPanel scoped to the selection). The 3D view adds no new metric
definitions — it reuses the THERMAL/POWER/CAPACITY numbers so pages agree.

### 7.3 Interaction
Orbit/pan/zoom (drei `CameraControls`), preset cameras (plan, cold aisle, hot
aisle, isometric), section cut along an aisle, hover tooltip, click select,
double-click focus, keyboard row/rack stepping, time scrubber with play/pause.
Accessibility: every 3D view has its 2D equivalent; the table under it lists
the same racks (Pagination component, standing rule).

### 7.4 Performance budget
Room scene ≤ 20 draw calls and ≥ 50 fps on integrated graphics; site scene
racks-only. `/twin` chunk lazy, three + r3f + drei ≈ 250–300 KB gz, never in the
main bundle. Scene JSON ETag'd; state payload ≤ 50 KB per room.

---

## 7.5 What was actually built (2026-10-07)

**Phase 0** (`c937bb6`, `48cc144`): migration 0099 geometry, stored aisles,
importer `--geometry-only` (the full importer unpins every endpoint's
collector shard - never run it on the live estate). **Phase 1** (`ab086a0`):
first 3D room view. **Viewer overhaul** (`3616b7b`, `df6394b`), after the
user supplied EkkoSoft Critical screenshots as the reference for the room
viewer itself (the page chrome stays ours): one WebGL scene with **3D | PLAN |
FPV**, racks graded bottom→top by the air at each third of the rack (server
inlets and door probes, `TwinDevice.temp_c`), air handlers graded
supply→return, discrete legend bands (ASHRAE A1 cut finer, tokens
`--band-t1..t8`, `--band-p1..p5`), a filters panel (rack layers: temperature,
compliance, RH, power usage, power utilisation with solid-to-used-share fill,
space, alarms; cooling layers: supply→return, supply, return, utilisation),
a room / rack / unit information panel, tile grid, vent tiles (assumed, one
per rack facing a cold aisle), grid references, containment, canvas-sprite
labels (no CDN font). Files: `features/floorplan/{layers.ts, Scene.tsx,
Stage.tsx, panels.tsx, FloorPlan.tsx, Plan2D.tsx, viewer.css}`.

Measured on the live estate: `/estate/rooms/{id}/kpi` is 69-160 s under
host memory pressure, so the viewer fetches it once per room, never polls
it. Rack load in `repositories/racks.py` is now what the rack PDUs meter -
summing every device counted each server twice.

**Still open from the reference:** timeline playback (Phase 4), capacity
tickets and the cooling advisor (not planned), rack faces showing their
devices (racks are solid colour; devices are drawn inside and hidden),
split screen, °F. **Known picture defects until S2:** the seven 1.75 m CRAHs
per hall overlap on 1.0 m centres and read as one long block; the hall's air
is uniform so the only gradient is the vertical 0-3 K ramp.

**Phase 3 (re-shaped) SHIPPED 2026-10-07** (`19abee1`, hotfix `2aacccf`): path
overlays in the viewer (POWER / COOLING trace back to the source, IMPACT; a
rack is traced through its rack PDUs; hops in other rooms listed, not drawn),
`/twin/sites/{id}` building picker (levels stacked in an oblique SVG
projection, rooms coloured by worst alarm, click to open), `room:{id}`
websocket topic from the ingest worker (one frame per room per batch, no
readings; the viewer re-reads the scene at most every 5 s on it, 60 s poll
as backstop). **Lesson:** `19abee1` defined the worker's helper below its
`__main__` block; `python -m app.ingest.worker` ran `main()` before the name
existed and every tick failed for ~8 minutes in production while the unit
tests (which import the module) stayed green. A test now pins that nothing
may follow the entry point.

**Phase 2 (re-shaped per §8a) BUILT 2026-10-08, not yet live** (the stack was
down for the RAM upgrade; verified against fixtures run through the real
backend code). Centre of gravity on the racks, as the reference reads them:
- **Rack layers** `inlet_outlet` (front face by intake tiers, rear face by
  exhaust tiers - the gradient shader took a second colour set and a
  per-instance front sign), `exhaust`, `variance` (spread of the intakes
  across the rack, K, `SPREAD_BANDS`).
- **Indices** in `core/thermal_indices.py`, pure: RCI_HI/LO (Herrlin 2005,
  clamped at 0, rated good ≥96 / acceptable ≥91 / poor), RTI (Herrlin 2007;
  equipment ΔT power-weighted as ΣP / Σ(P/ΔT), unit weight where power is
  missing and the count published), SHI/RHI (Sharma 2002; reference = mean
  supply of the non-offline air handlers). Capture Index deliberately not
  shown. Attributed per the probe-first rule: rack-front probes and server
  inlets are intakes, server exhausts and rack-rear probes are exhausts, a
  rack PDU's ambient probe is neither. Scored against the room's
  `ashrae_class` (now on the floor plan). Served inside the scene as
  `indices` (room + per rack); shown in the room panel (tiles RCI HI / RCI LO
  / RTI, verdicts, evidence counts), the rack panel (intake/exhaust max,
  spread, SHI/RHI, rise, hot-spot verdict) and the status bar.
- **Hot spots**: a rack whose max intake is above the recommended ceiling
  (`allowable`) or the allowable one (`out`); cones over them in every layer,
  toggle in Visibility.
- **Heat map** (the optional toggle §8a asked for): `GET
  /twin/rooms/{id}/field` → `core/thermal_field.py`, pure Python IDW (p = 2,
  3D distance) PER AISLE: cold-aisle cells from the intake face points that
  open onto that aisle, hot-aisle cells from the exhaust face points; never
  across a row; cells under racks and in unclassed aisles stay unpainted.
  Three planes at 0.5 / 1.2 / 1.8 m, 0.3 m cells, readings collapsed to one
  point per face per rack third. Each cell carries a confidence from the
  distance to its nearest sensor; the viewer draws the plane as a
  `DataTexture` with the legend's band colour and confidence as opacity, so
  unmeasured air fades rather than being painted. Fetched only while a plane
  is showing (`?heat=bottom|mid|top`), ~85 KB for an 18 × 12 m hall.
- Tests `tests/test_twin_thermal.py` pin the arithmetic (hand-worked RCI /
  RTI / SHI cases) and the one rule that matters: an exhaust reading never
  reaches a cold-aisle cell.
- **Known limits until S2:** the simulator's air is uniform per room, so on
  the live estate every rack will read the same band and the heat map will
  be flat; RTI per air handler (D6 said "room, CRAH") needs per-unit rack
  assignment and waits for the influence matrix (Phase 5). The headline
  acceptance scenario (§8, trip one CRAH) cannot pass before S2 and was not
  attempted.

**Phase 4 BUILT 2026-10-08, not yet live** (same stack-down day; verified
against fixtures run through the backend code plus a mock frame endpoint).
- **Frame**: `GET /twin/rooms/{id}/frame?t=` → `core/twin_frame.py`, pure:
  the live scene re-filled from history. Source by the moment's age, the
  thermal-trend rule: newest 2 h from `telemetry_sample`, ≤ 2 days from
  `telemetry_5m` (`last_value` in bucket), older from `telemetry_1h`; a
  reading counts if it is the last within a lookback (15 min / 15 min / 3 h).
  Severities = alarms with `first_seen <= t` and not cleared by t
  (`max(severity)`, enum order). Rack roll-up recomputed from the frame
  (load = rack PDUs' `power_draw`, hottest intake, worst severity,
  `offline_count` 0). **Communication status has no history here** - the
  frame says so in `frame.note`; `poll_result` is 14 days and per endpoint,
  out of scope. Geometry untouched (D4). `GET …/field?t=` replays the heat
  map the same way. Same `TwinRoomScene` shape + `frame` block, so the viewer
  draws it with the Phase 1-3 code unchanged.
- **Timeline bar** (`features/floorplan/Timeline.tsx`, per the reference's
  bottom bar and §8a): Live toggle, date, step back / play / step forward
  (inline SVG glyphs), a 24 h scrubber at 5 min steps with hour ticks and the
  time riding on the thumb, the day's future greyed past "now", a source
  note ("from the 5-minute record"), and **Compare** (1 h / 24 h / 7 d
  earlier, or the live room) as a second canvas beside the first with
  captions. URL carries `t=` and `cmp=`; playback steps 5 min every 1.5 s
  and prefetches the next frame; the `room:{id}` topic and the 60 s poll are
  off while replaying. Status bar: accent dot + "Replay · <when> · <source>".
  Built with the ui-ux-pro-max + frontend-design skills (user rule
  2026-10-08): native range (keyboard-operable), step buttons as the
  single-pointer alternative to dragging, visible focus rings, disabled
  states at 0.45 opacity, no emoji glyphs, reduced-motion respected.
- Tests `tests/test_twin_frame.py` (9) pin source choice, the re-fill
  (silent sensor → no reading, live status kept, severities replaced), the
  rack roll-up and the enum ranking.
- **Extras, same day:**
  - **Independent compare time.** "Pick a time" in Compare adds a slimmer second row with its own date, step buttons and scrubber, and a dashed secondary-colour thumb. The URL carries `cmp=pick&cmp_t=`; the offset presets are unchanged.
  - **History range.** `GET /twin/rooms/{id}/history` returns the oldest hourly bucket of any device in the room (an index read on the hourly rollup), cached 10 min in-process. The date picker stops there, the scrubber greys the hours before it, and a shared link to an earlier moment snaps to it.
  - **Step size.** 5 min, 15 min or 1 h per frame, used by play and the step buttons; the URL carries `step=`.
  - Two more tests in `tests/test_twin_frame.py` pin the cache and the empty case.

**Small items 2026-10-08 (same tree):**
- **°C / °F** switch beside RESET (`features/floorplan/units.ts`, per viewer in
  localStorage). Data, bands and indices stay °C; every printed temperature
  goes through `fmtT`, differences through `fmtDT` (K, or °F of difference),
  and legends relabel from their band edges.
- **See into racks** (Visibility): the cabinet becomes a tinted shell at the
  layer's colour and the devices inside show; hover and click pass through
  the shell to the devices.
- **Ingest-lag hysteresis** (`alarms/platform.py`): an open `ingest_lag_high`
  is held (at warning once under 60 s) until the lag has stayed under 30 s
  for 2 min; a pop back over the line while open needs no new dwell. The
  worker tracks time under the clear line; the monitor reads the open
  alarm's severity. Answers the 2026-10-07 drain that flapped the banner.
- **Reserved / allocated / measured power.** `repositories/racks.room_rack_power_plan`:
  allocated = Σ over devices physically in the rack (installed / in service /
  maintenance; rack PDUs and sensors excluded; `planned` placeholders
  excluded so a reservation is not counted twice) of `model.attributes.budget_w`,
  else nameplate × `BUDGET_DERATE` 0.6 (a common planning default, not a
  standard; sites set their own per model). Reserved = Σ `capacity_reservation.power_kw`
  with status `held`. On `FloorRack` as `allocated_kw`, `reserved_kw` and the
  derated / unrated counts. Viewer: rack layer **Committed power** =
  (allocated + reserved) / rating on the % bands with solid fill to that
  share; rack panel **bullet bar** - track = one feed's rating with 80 / 90 %
  marks, body = allocated + reserved (hatched), thin bar = measured, with an
  "over by" line when committed exceeds the rating.
- **Unit conditions in the panel.** Device and plant-unit panels list the
  unit's open conditions (symptoms included, worst first, severity as a word
  and a left rule) with **Acknowledge** and **Clear** on the existing audited
  endpoints; clear confirms inline ("it reopens if the condition is still
  there"); 409 / 403 explained in words. No ad-hoc shelving: suppression is
  planned work (ISA-18.2), so the panel offers **Plan maintenance on this
  unit**, which opens the maintenance form with the device picked
  (`/maintenance?schedule=<id>`, new `initialDeviceIds` on WindowForm).

## 8. Phases

| Phase | Scope | Size | Depends on |
|---|---|---|---|
| **S1** | Simulator geometry export: building block, u_height, mounting, plant coords, defect fixes | S | — |
| **0** | Platform data foundation: migrations §4, importer §5.1, 2D plan fixes §5.3, `/twin/*/scene` | M | S1 |
| **1** | 3D room view: route, instanced racks/devices/plant, aisles + containment, alarm/power/space/rack-inlet layers, select → elevation/drawer, 15 s polling | L | 0 |
| **S2** | Simulator spatial thermal model + sensors + tests + dataset regen | M–L | S1 |
| **2** | Thermal field (3 planes, per-aisle, confidence fade) + RCI/RTI/SHI | M | 1, S2 |
| **3** | Site/level view, path and impact overlays in 3D, `room:{id}` WS topic | M | 1 |
| **4** | Time playback from caggs | M | 2 |
| **5** | Zones of influence + CRAH-failure what-if (fitted, labelled as estimate) | L | S2, 4 weeks+ of history |
| **6** | Room builder (manual geometry edits, `geometry_source='manual'`) | L | 0 |
| **S3** | Simulator building fabric, support rooms, door contacts, the compound and the moved rooms, floorplan 1.2 (§8b.3, §8c.3) | M–L | S1; ships with S2's regeneration |
| **7** | One viewer: building rung inside the floor plan, breadcrumb, `space` + doors, Building page retired (§8b.4) | M–L | 0; full value needs S3 |
| **8** | Per-level drawing import under the fabric (§8b.5) | M | 7 |

Each phase ends with live verification against the running stack, the
standing rules (CI green, chart/table rules, ship-frontend rule) and the
estate pages agreeing with the twin to the decimal.

**Acceptance for the headline scenario** (Phase 2): trip one CRAH in DC2 Server
Hall A in the simulator → within the NOW horizon the racks nearest that unit go
amber/red in the 3D inlet layer and the mid plane shows a local hot zone, racks
across the hall stay green, RCI_HI drops, the THERMAL page shows the same racks
— and the hall in DC1 does not change.

---

## 8a. Amendments after the reference screenshots (2026-10-07)

The user supplied 37 EkkoSoft Critical screenshots as the reference for the
room viewer. Three things in them move the plan: the reference reads the room
off the **racks**, not off interpolated air; its viewer carries a **timeline
and an editor** our phases put late; and some of its layers need data the
simulator does not yet expose.

| Phase | Change |
|---|---|
| **S2** | Unchanged in substance. Two more outputs: per-rack **exhaust** (for an inlet/outlet layer) and per-rack **airflow** (the simulator already derives it from power). CRAH re-grid stays. |
| **2** | Centre of gravity moves from the room's air to the racks. The three-height interpolated planes become an optional "heat map" toggle. The deliverable is the extra rack layers (inlet/outlet, temperature variance, smooth "temperature heatmap"), the ASHRAE scores (RCI, RTI, SHI/RHI) in the room and unit panels, and hot-spot markers. |
| **3** | The building view becomes a light site → levels → rooms picker that opens the viewer (the reference has no building scene; its site level picks a room). Path overlays and the `room:{id}` push topic stay. |
| **4** | Design fixed by the reference's bottom bar: Live toggle, date, play, scrubber, split screen (two times side by side). Data side unchanged. |
| **5** | "Cooling zones" maps straight onto the influence matrix. Add a read-only cooling advisor (setpoint / vent recommendations, accept or dismiss) on the what-if. |
| **6** | Grows to the reference's Editor and Capacity tabs: reposition a rack; capacity tickets (new rack, add to rack, decommission) with accept-and-place, on the existing `capacity_reservation` table. |

New, small, unphased: rack faces showing their devices; a reserved /
allocated / measured power layer; °C / °F; per-unit alarm management in the
panel. Unchanged: no CFD, the ingest-lag fix, S2's dependency on the RAM
upgrade.

## 8b. One viewer, building first (2026-10-08)

Written against dcim-platform `708c1bd` and simulator `669ff90`, after the user
asked why the building has its own page and whether the model should show
operator rooms, stores, passages and doors. Nothing in this section is built.

### 8b.0 The verdict

1. **The Building page goes.** It is a picker in an oblique SVG - stacked
   levels, click a room, land in the viewer - and it is the only rung of the
   site → building → level → room ladder that lives on its own route. Every
   reference product (Sunbird dcTrack, Nlyte, Schneider EcoStruxure IT,
   Hyperview, NetEco) walks that ladder inside one spatial viewer with a
   breadcrumb. So does ours from now on: the floor plan opens on the
   building, a click descends, the breadcrumb ascends.

2. **The building is the default, deep links are not.** `/floorplan` with no
   room shows the building. `/floorplan?room=…` shows the room, exactly as
   today, because every alarm row, rack page and world-map tile links to a
   room and nobody arriving from an alarm wants a second click.

3. **"Realistic" means "has data", not "looks like an architect's render".**
   A real DCIM does not model offices, corridors or doors as objects; it lays
   the architect's drawing (DWG/DXF/PDF) under the monitored rooms and places
   assets on it. Corridors and doors come from the drawing. The twin keeps
   its rule - it draws what has data - and gets its fabric from a drawing
   layer, not from invented rooms with nothing in them. Spaces that *do*
   carry assets or telemetry in a real facility are added as rooms, with
   their data. Doors are drawn as monitored door contacts where the BMS has
   one, and as openings in the drawing where it does not.

### 8b.1 Design decisions

| # | Decision |
|---|---|
| **B1** | One route. `/twin/sites/:id` redirects to `/floorplan?site=:id`. `features/site/Building.tsx` and `building.css` are deleted. |
| **B2** | Rung from the URL: `site` (datacenter id; default = first site, remembered per viewer), `level` (optional: focus one slab, ghost the others), `room` (present = room rung; the existing `view`, `layer`, `cooling`, `heat`, `t`, `cmp`, `step` keep their meaning there). Breadcrumb `DC1 › Level 1 › Server Hall A`; each crumb is a link that drops the keys to its right. |
| **B3** | The building rung is drawn in the **same three.js scene** as the room, at coarse level of detail: level slabs at their elevation (data: `levels[].elevation_m`, `floor_to_floor_m`), room volumes (`width_m × depth_m × (floor_to_floor − slab)`) with 12 % faces coloured by the room's worst open condition, free-standing plant as its true footprint (data exists since S1), rack rows as flat blocks (positions exist). No per-device meshes, no heat field, no cable paths at this rung. Target under 5 k triangles plus two instanced meshes. |
| **B4** | Entering a room is a camera move, not a page load: the room's volume is framed, the room rung's scene query runs (`/twin/rooms/{id}/scene`), and the building geometry stays as a ghost while the detail streams in. Esc or the breadcrumb returns; the building geometry is still in memory. The SVG fallback (no WebGL) gets a 2D stack of level plans with the same colouring. |
| **B5** | Three kinds of space, told apart in data and on screen: **monitored rooms** (today's `room` rows, `room_class` `white_space` / `facility`); **support rooms** (new `room_class = 'support'`: rooms that hold assets or a sensor but no racks of IT - see 8b.3); **fabric** (new `space` table: corridor, lobby, loading dock, stair/lift core, entrance, external wall, door opening). Fabric is drawn flat and grey with a label and is **never coloured by state**, because it has none. |
| **B6** | Doors. A monitored door is a BMS binary input (`door_open`) on the plant controller, one per hall, electrical room and meet-me room door, with a `door_held_open` condition after 120 s and a platform alarm in the `security` category (new category; see Q6). It is drawn as a door leaf on the wall at the position the fabric gives, coloured by state and alarm. An unmonitored door is an opening in the fabric, uncoloured. The two never look alike. |
| **B7** | Geometry is computed in the importer and stored, never derived per request. The site scene already comes from `datacenter.attributes.building` plus `room`; it grows by `space` rows and door points. The host is memory-tight; the API stays a read. |
| **B8** | Nothing is generated that nothing reads. No NOC without a sensor, no store without stock, no corridor coloured by anything. |

Q2 in §9 ("separate TWIN nav entry, or a toggle on the floor plan, plus a
site-level entry for the building") is answered by B1: one entry, the floor
plan, building first.

### 8b.2 What the live building is (verified 2026-10-08)

Both sites import the same envelope today: levels G (0 m), 1 (5 m), 2 (10 m),
Roof (15 m); outline of four points; eight rooms:

| Level | Room | Class | Size (m) | Origin (m) |
|---|---|---|---|---|
| G | UPS Room | facility | 9.0 × 6.0 | 0, 0 |
| G | Mechanical Room | facility | 8.0 × 4.0 | 0, 6 |
| G | Generator Room | facility | 10.0 × 12.6 | 9, 0 |
| 1 | Server Hall A | white_space | 12.2 × 12.3 | 0, 0 |
| 1 | Network Room | white_space | 3.0 × 5.4 | 12.2, 0 |
| 1 | Central Plant | facility | 16.0 × 15.0 | 15.2, 0 |
| 2 | Server Hall B | white_space | 12.2 × 12.3 | 0, 0 |
| Roof | Roof | facility | 12.0 × 6.0 | 0, 0 |

Two things the building rung will make visible that the room rung hides: the
Central Plant sits on level 1 beside the hall (S1 placed the runtime hall
"beside its level's rooms"; a chiller plant on the first floor is odd but is
what the geometry says, and the view should show the geometry, not flatter
it); and level 2 is a single hall with nothing beside it. Both are inputs to
S3, not things the viewer should paper over.

### 8b.3 Simulator Phase S3 - building fabric and data-bearing spaces

All of it in `core/equipment_geometry.complete_building()` and the topology,
exported in `floorplan` **1.2**. Needs Stop → Regenerate Datasets → Start, so
it ships with S2's regeneration, after the RAM upgrade, not before.

**Fabric** (`floorplan.fabric[]`, each `{level, kind, name, polygon_m, attrs}`),
derived from the room layout so it stays right when the fleet opens a hall:

- a 2.4 m corridor along the open side of each level's rooms, joining them;
- a 3 × 6 m stair-and-lift core at the same plan position on every level;
- on G: a lobby at the entrance and a loading dock against the generator room
  wall (a real dock sits where the heavy kit arrives);
- the external wall as the outline, with one entrance opening and one dock
  door; a door opening in each room's corridor wall (`kind: 'door'`,
  `attrs.room`, `attrs.monitored: bool`).

**Support rooms** (`room_class: 'support'`), each with what makes it real:

| Room | Level | What it holds | Protocol / data |
|---|---|---|---|
| Staging & Storage | G | Devices in lifecycle `planned` / `stock`: the fleet lifecycle moves a device from here into a rack on install, and back on decommission. Asset records, no telemetry. | Asset lifecycle only |
| Operations Room (NOC) | 1 | One temperature / RH sensor; the wall display's home. | SNMP sensor |
| Meet-Me Room | 1 | Two carrier handoff routers and an ODF, on the OOB plane. | SNMP / gNMI |
| UPS Room (existing) | G | Gains battery string temperature and hydrogen concentration, which a UPS room with batteries is required to monitor. | BACnet AI on the plant controller |

**Doors**: `door_<room>_open` BACnet binary inputs on the plant controller for
Server Hall A/B, UPS Room, Generator Room, Meet-Me Room (the rooms a NOC
alarms on). Normally closed; the lifecycle opens one for 20-200 s when it
installs or decommissions in that room, and the scenario tool can hold one
open. Export each as a device of type `sensor` with metric `door_open`
(bool), rack-less, `mounting: 'wall'`, positioned on the door opening.

### 8b.4 Platform Phase 7 - the building rung

**Data (migration 0102).** `space` table: `id`, `datacenter_id`, `level`,
`kind` (`corridor | lobby | dock | core | entrance | wall | door`), `name`,
`polygon_m` (jsonb, metres in the site frame), `attributes`. `room.room_class`
gains `support`. New metric key `door_open` (the five steps in the
adding-a-metric-key rule: registry, codegen, migration marking it hot,
collector rebuild, importer default). Alarm category `security` with the
`door_held_open` condition.

**Importer.** Reads floorplan 1.2: upserts `space`, support rooms, door
sensors. `--geometry-only` covers `space` too.

**API.** `/twin/sites/{id}/scene` gains `fabric: Space[]` and
`doors: {device_id, room_id, level, x_m, y_m, facing_deg, open: bool|null,
max_severity}[]`; `rooms[].room_class` may be `support`. Nothing else changes.

**Frontend.**
- `FloorPlan.tsx`: rung state from the URL (B2); site picker when the estate
  has more than one site; breadcrumb; the room `<select>` becomes the level 3
  crumb. The old `/twin/sites/:id` route redirects.
- `Scene.tsx`: a `BuildingRung` group (slabs, room volumes, fabric, plant
  footprints, rack blocks, doors) and a camera transition into a room; the
  room rung as today. Hover on a room volume shows name, class, worst
  condition, racks, devices; hover on a door shows state and age.
- Panel at the building rung: the site's KPI card (`/sites/{id}/kpi`: PUE,
  utilisation, outdoor air - already built for the world map), then rooms by
  level with their worst condition, support rooms and fabric listed but
  unlit. The world map's "Building" tile opens `/floorplan?site=`, its
  "Floor map" tile opens the hall as now.
- Layers at the building rung: alarm (default), power per room (from the
  room KPI), space per room. Thermal and cooling layers are room-rung only.
- SVG fallback: stacked level plans, same colouring, click to enter.

**Acceptance.**
1. `/floorplan` with no parameters shows DC1's building: four slabs, eight
   room volumes in their stored positions, fabric grey, plant footprints at
   true size; the breadcrumb reads `DC1`.
2. Click Server Hall A: the camera frames it, the breadcrumb reads
   `DC1 › Level 1 › Server Hall A`, the URL carries `room=`, and the view is
   the room viewer as it is today, layers and timeline included.
3. An alarm-row link to a room opens the room directly; `Esc` or the `DC1`
   crumb returns to the building without a reload.
4. A door held open in the simulator turns its leaf red within one poll,
   raises a `security` alarm that the alarm list and the room's panel show,
   and clears on close; a fabric door opening never changes colour.
5. A device in `stock` sits in Staging & Storage on the building rung and
   moves to its rack when the lifecycle installs it.
6. `/twin/sites/:id` redirects; the Building component no longer exists;
   type-check, build and the full backend suite pass; the estate pages agree
   with the twin to the decimal.
7. Building rung first paint under 300 ms after the scene query on the dev
   host; room entry streams without a blank frame.

**Phase 7, first increment, SHIPPED 2026-10-08 (`62e7da0`)** on today's data:
B1-B4 and the panel. One route, building by default, room one click down
with pushed history, breadcrumb, site picker, level filter, 3D and PLAN,
oblique SVG without WebGL, `/twin/sites/:id` redirect, Building page
deleted. `/twin/sites/{id}/scene` now carries each room's racks and placed
plant from two site-wide reads, plus empty `fabric` and `doors`. Still owed
from §8b.4: migration 0102 (`space`, `support`, `door_open`, `security`),
importer and API for fabric and doors, the power and space layers at the
building rung, and the door and support-room drawing - all waiting on the
simulator's S3 export.

### 8b.5 Phase 8 - drawing import (later, real sites)

A per-level architectural drawing (SVG first; DXF via a converter) with scale
and offset, stored in `level_drawing`, drawn under the fabric and rooms, and
toggleable. This is how a real customer's corridors, offices and doors
arrive, and it is the reason B5 keeps fabric separate from rooms: the
drawing replaces generated fabric, never the monitored rooms.

### 8b.6 Open questions

- **Q5** Battery monitoring: hydrogen and string temperature on the UPS Room
  (VRLA in cabinets, the common case) or a separate Battery Room (Li-ion,
  the growing case)? Recommendation: UPS Room now; a battery room is a
  topology option later.
- **Q6** Door alarms: new `security` category, or under `environment`?
  Recommendation: `security`; a NOC routes it differently.
- **Q7** Single-site deployments: open on the building or on the only hall?
  Recommendation: the building, with the viewer's choice remembered.
- **Q8** Fabric generated from the rooms (S3) or curated per topology?
  Recommendation: generated, with a curated override file, so the fleet can
  open a hall without an architect.

### 8b.7 What this does not do

No architectural render; no furniture, people or vehicles; no corridor,
office or door coloured by anything it cannot measure; no building geometry
computed per request; no change to the room rung's layers, timeline or
what-if.

## 8c. The compound: a site rung above the building (2026-10-08)

Amends §8b after the user asked whether the Central Plant and the Generator
Room belong inside the building at all. Verified against the live scene:
the Generator Room (two 2.6 × 6.6 m gensets with paralleling switchgear) is
on the ground floor inside; the Central Plant (three water-cooled chillers,
seven pumps) is on level 1 beside Server Hall A; the cooling towers are on
the roof; the UPS Room (two UPS, two ATS, switchgear) is on the ground floor.

### 8c.0 The verdict

1. **Standby gensets of this size live in a yard, not a room.** Nearly every
   enterprise and colocation site puts them outside in weatherproof
   enclosures behind a fence, with bulk diesel, day tanks and fuel polishing.
   Indoor generator halls are the urban, land-starved exception and need
   exhaust stacks, louvres and a fire compartment. For a two-building
   campus the yard is the normal answer; the paralleling switchgear stays
   indoors in an electrical room.
2. **Water-cooled chillers belong indoors, on the ground.** A plant room is
   right; level 1 is not. A chiller is 8-15 t, vibrates and needs condenser
   water risers and drainage, so plant rooms sit on the ground floor or in a
   separate energy centre. Level 1 is S1's "beside its level's rooms" rule
   placing a runtime room, not a design.
3. **Cooling towers on the roof are fine**; a screened yard beside the plant
   room is equally common. If the plant moves to the ground, the yard pairs
   better, but the roof stays defensible and is kept.
4. **The model has no compound.** A site is the building, nothing else. A
   real site has a fenced substation with the utility transformers and MV
   intake, a generator yard with bulk fuel, makeup-water storage for the
   towers (a day or two), a fire water tank and pump house, two diverse
   telecom entrances into the meet-me room, and the fence, gate, guardhouse,
   car park and loading apron around them. None of it is modelled.

### 8c.1 Decisions (amending §8b)

| # | Decision |
|---|---|
| **C1** | **A site rung above the building.** The floor plan's ladder becomes site → building → level → room. `/floorplan?site=` opens on the compound: the ground plane with the site outline, the outdoor zones, and the building extruded with its levels visible through it. Clicking the building descends to the building rung (§8b B3); the breadcrumb climbs. `building=` in the URL names the building when a site has more than one. |
| **C2** | **Outdoor zones are `space` rows too.** §8b B5's `space.kind` gains the outdoor kinds `generator_yard`, `substation`, `cooling_yard`, `fuel_tank`, `water_tank`, `fire_pump_house`, `guardhouse`, `parking`, `apron`, `fence`, `gate`, `telecom_entrance`. Outdoor fabric is drawn flat on the ground plane in the same grey as indoor fabric and is never coloured by state. |
| **C3** | **Equipment outdoors stands in an `outdoor` room.** A yard that holds monitored equipment (gensets, transformers, towers, tanks) is a `room` row with `room_class = 'outdoor'`, `level = 'Site'`, elevation 0 and no walls, so the alarm roll-up, the room KPI, the paths and the twin's room rung keep working unchanged. A yard without equipment is fabric only. |
| **C4** | **The simulator moves the rooms.** Generator Room → Generator Yard (`outdoor`), plus a bulk fuel tank with a level transmitter and the utility substation with two transformers. Central Plant → level G. Roof stays. Device names keep their room codes (`GR`, `CP`, `RF`): the platform keys device identity on them, and a rename would churn every device at both sites. The room's *name* and *class* change; its *code* does not. |
| **C5** | **One geometry pass.** The compound outline, outdoor zones and the moved rooms ship in the same simulator export (floorplan 1.2) and the same platform migration (0102) as §8b's fabric, support rooms and doors. The dataset regeneration they all need happens once, with S2's. |
| **C6** | **Nothing simulated for the sake of the picture.** Fence, gate, apron and car park are drawing only. The substation's transformers, the fuel tank's level, the water tank's level and the guardhouse door are modelled only because a real site monitors them (transformer temperature over SNMP or Modbus from a protection relay or a temperature monitor; tank levels over Modbus or BACnet from a level transmitter; the guardhouse door through the BMS). |

### 8c.2 What the compound holds, and how it is known

| Zone | Holds | Protocol / data | Why a NOC sees it |
|---|---|---|---|
| Substation | Two utility transformers, MV switchgear | Transformer winding temperature and load via SNMP (temperature monitor) or Modbus (protection relay); the intake meter already exists | Loss of a feed is the first event in most outages |
| Generator Yard | The two gensets (moved), day tanks | As today (Modbus genset controllers) | Run status, fuel, failure to start |
| Fuel tank | Bulk diesel tank, level transmitter | Modbus or BACnet AI, level % and litres; low-level alarm | Runtime at full load is the number the NOC asks for |
| Cooling yard / Roof | Towers (roof, unchanged); makeup-water tank | Tank level AI on the plant controller | Tower makeup is the single point that empties a plant |
| Fire pump house | Fire pump controller | BACnet BI run/fault, or dry contacts | Life safety; monitored, never controlled |
| Telecom entrance | Two diverse duct routes to the meet-me room | Drawing only; the carriers' handoffs are in the MMR (§8b.3) | Diversity is an audit question, not a live one |
| Guardhouse, gate, fence, parking, apron | Fabric; the guardhouse door through the BMS | Door BI (§8b B6) | Access |

### 8c.3 Simulator S3, amended scope

`core/equipment_geometry.complete_building()` grows into `complete_site()`:
the building as before, plus a `compound` block in floorplan 1.2:

```
floorplan.compound = {
  outline_m: [[x, y], ...],            # the fenced site, metres, same frame as the building
  buildings: [{code, outline_m, levels}],
  zones: [{id, kind, name, polygon_m, room?}],   # room set when the zone is an outdoor room
}
floorplan.fabric = [{id, level, kind, name, polygon_m}]   # indoor, from §8b.3
```

Site layout, derived from the building outline so a fleet-opened hall still
fits: the building at the centre of a site roughly three times its
footprint; a 3 m service road and apron along the dock side; the generator
yard and fuel tank on the dock side beyond the apron; the substation on the
far side from the halls, at the utility entrance; the makeup-water tank and
fire pump house beside the plant room's wall; the guardhouse at the gate;
two telecom entrance routes from opposite fence lines to the meet-me room.

Room changes in `topologies/dual_dc_enterprise.json` by an idempotent tool
(`tools/compound_and_support_rooms.py --check`), in the style of
`tools/layout_buildings.py`: Generator Room → Generator Yard, class
`outdoor`, level `Site`; Central Plant → level G, beside the UPS and
Mechanical rooms; the §8b.3 support rooms added; new devices: two utility
transformers (substation), one bulk fuel tank with a level transmitter, one
makeup-water tank with a level transmitter, one fire pump controller, door
contacts and the UPS Room's hydrogen and battery temperature points on the
plant controller. New points follow the adding-a-metric-key rule in both
repositories.

### 8c.4 Platform Phase 7, amended scope

Migration 0102 as in §8b.4, with `room_class` also admitting `outdoor` and
`space.kind` admitting the outdoor kinds. `/twin/sites/{id}/scene` gains
`compound: {outline_m, zones[]}`. The site rung in the viewer: ground plane,
fence line, zones in fabric grey, outdoor rooms with their equipment at true
footprint, the building extruded with ghosted levels; click the building for
the building rung. The breadcrumb gains the site crumb's second role: at the
site rung it is the title, at the building rung it climbs.

**Phase 7 second increment and 0102 SHIPPED 2026-10-13 (`e77057f`, migrated
live).** Migration 0102 (`space`, `support`/`outdoor`), importer for
floorplan 1.2 (empty support rooms, spaces, compound outline, vanished-room
prune), `/twin/sites/{id}/scene` with `fabric` and `compound`, and the site
rung: the floor plan opens on the compound, the building is a shell to
click, fabric and door openings draw on the slabs. The live estate shows no
compound until the simulator is regenerated (it still exports 1.1). Still
owed: doors as BMS contacts, the `door_open` metric and the `security`
category, the power and space layers at the building rung - all waiting on
S3's second increment.

**S3 increment 2, part A, CODED 2026-10-13 (simulator + platform migration
0103).** Door contacts on the halls, Network, UPS and Meet-Me rooms; a
hydrogen detector and a battery string probe in the UPS Room; level
transmitters on the bulk diesel and makeup water tanks; the operations room's
T/RH - all Modbus instruments behind the BMS gateway, with the tank, gas and
string dynamics in the state store, a lifecycle door hook and two scenario
endpoints. Platform: registry keys, generated templates, a `security` category,
rules at the standards' numbers (0103). See the simulator's
`docs/S3_COMPOUND_AND_FABRIC.md`. Owed from §8b.3/§8c.3: the `transformer`
device type, the door leaves coloured by state in the viewer, and the
regeneration that puts all of it on the wire.

**S3 increment 2, part A ON THE WIRE 2026-10-13.** Simulator regenerated on
`bbc0d61` (704 devices, 586 SNMP / 46 gNMI datasets, five protocols), full
import: both sites 11 rooms, fabric 19, compound 65 × 45 m with 13 zones,
10 door contacts and every instrument profiled by the BMS collectors
(SIM-DOORCT/H2DET/BATTEMP/FUELTANK/WATERTANK), samples landing, zero
collector errors. Door bits go to `telemetry_bool` on change plus heartbeat;
`/devices/{id}/metrics` had read only `telemetry_sample` and now reads all
three tables.

**S3 increment 2, part B, CODED 2026-10-13 (simulator + platform migration
0104, platform `b9a4008`).** Q10 answered as recommended: one transformer per
site, no MV switchgear device. Simulator: a `Substation` outdoor room on the
west margin (the drawn zone becomes the room), `TX1-<DC>-SB` as device type
`transformer` - the tank monitor on Modbus TCP (`SIM-XFMR-ITM-v1`), no SNMP,
a power edge above the revenue meter - with IEC 60076-7's thermal model over
the site's outdoor air, the ONAF fans and the monitor's relays
(`tools/add_substation.py`, idempotent). Platform: the device type, keys
`oil_temperature` / `winding_temperature` / `tap_position`, the generated
template, plant-page readers, rules at the relays' settings (85/95, 105/115 C)
and the load rule learning the type. The viewer draws each monitored opening
as a door leaf from the scene's `doors` (whatever reports `door_open`, by
room): in the wall when closed, swung into the room when open, alarm-coloured
when held. Simulator committed `5c765fd`, regenerated and fully re-imported the same
day: 12 rooms per site, Modbus 74 devices, both monitors answering (a new
management address needs `POST /api/binding/bind` after the topology upload,
then a Modbus restart). Owed: the power and space layers at the building rung
(Phase 7 remainder).

**Phase 7 remainder, the building rung's layers, BUILT 2026-10-13.** The site
scene's rooms carry `power` (metered at the rack PDUs, rated, allocated,
reserved) and `space` (rack units filled and fitted), each the sum of the
room's racks by the rack page's own rules, in one read for the site
(`site_room_capacity`). The site and building rungs gain a Layer switch -
Alarms, Power, Committed, Space - sharing the room rung's `layer` keys, bands
and legends, so the colouring survives the descent into a room and the climb
back. A room with no racks, or racks with no rating, keeps the "no reading"
tone and a dash in the panel: it is not drawn as an idle room. Checked against
the estate's database: the halls read 57 % of 125 kW metered, 75 % committed
and 54 % of 336 U; the plant and support rooms read nothing.

### 8c.5 Acceptance additions

8. `/floorplan?site=` opens on the compound: outline, substation, generator
   yard with both gensets, fuel tank, water tank, fire pump house, the
   building. The gensets are no longer inside the building.
9. The Central Plant is on the ground floor; its three chillers and seven
   pumps are where they were relative to each other.
10. Every device keeps its name and id across the move; the platform's
    device count does not change except for the devices added, and no
    device's history is broken.
11. A fuel tank below its low-level threshold raises an alarm that names the
    runtime it implies.

### 8c.6 Open questions

- **Q9** Fuel: one bulk tank with two gensets drawing from it (simplest, common
  for 2 N gensets), or one tank per genset (N+1 sites often do)? Recommendation:
  one bulk tank, two day tanks drawn as fabric.
- **Q10** Substation: model the MV switchgear as a device (Modbus from a relay)
  or leave only the transformers? Recommendation: transformers only now.

## 8d. Phase 6, the room builder (started 2026-10-13)

Q3 answered yes: a real deployment has to be able to correct its drawing.

### 8d.0 What the editor is, and is not

No protocol reports where a rack stands. In production the drawing comes from
CAD or a survey and is then maintained by hand in the DCIM (Sunbird dcTrack,
Nlyte and NetEco all carry a floor editor; EkkoSense builds every room by
hand). Two things follow.

1. **An edit corrects a record; it moves nothing.** Dragging a rack says "the
   drawing was wrong" or "this move has been carried out". A planned move is
   a work order - the capacity-ticket half of Phase 6, on
   `capacity_reservation` - and is not built here.
2. **The rules are the drawing's, not a code's.** A footprint stays inside its
   room and off every other footprint. Service clearances and aisle widths are
   not judged: they depend on the equipment and on local rules, and a tool
   that refused a real, non-compliant floor could not be made to match it.

### 8d.1 Increment 1, backend (BUILT 2026-10-13, not deployed)

- `core/placement.py`, pure: `footprint()` (quarter turns exact, any other
  angle bounded), `problems()` (outside the room; overlaps, named), `snap()`
  to the 600 mm tile. Racks in a row touch; a centimetre of tolerance - the
  resolution of a stored coordinate - keeps touching from reading as overlap.
- `PUT /twin/racks/{id}/placement` `{x, y}` and
  `PUT /twin/devices/{id}/placement` `{x, y, rotation_deg?}` (floor-mounted
  units with a footprint only): operator and up, 409 with the named problems
  when refused, `geometry_source = 'manual'` on success, audited as
  `geometry.place` with before and after.
- `DELETE /twin/{racks|devices}/{id}/placement` hands the object back to the
  import (`geometry.release`); the position holds until the next geometry
  import, which then owns it again.
- Nothing new in the schema: migration 0099 added `geometry_source` and every
  geometry import has left `'manual'` rows alone since.
- Checked read-only against the estate: Server Hall A's imported drawing has
  17 footprints and no overlaps; a rack is accepted where it stands, refused
  onto its neighbour and refused through the wall.

### 8d.2 Owed

- **The editor itself** (frontend): an Edit mode in the PLAN view only -
  top-down, orthographic, the view a floor is drawn in - with drag, tile snap,
  a live ghost that turns to the alarm colour where the server would refuse,
  Save and Discard, and a mark on every hand-placed object with "Hand back to
  the import". Not in 3D or FPV: nobody surveys a floor in perspective.
- **Aisles after a move.** Stored aisles are the import's; a rack moved out of
  its row leaves them stale. Either re-derive from rack facing when a room has
  manual racks, or let the editor draw aisles.
- **Room size and origin** by hand (`room.geometry_source` is honoured by the
  importer, nothing writes it).
- **Rack facing and footprint** edits; adding and removing racks.
- **Capacity tickets**: new rack, add to rack, decommission, with
  accept-and-place.
- **The simulator's air model reads its own geometry**, not the platform's: a
  rack moved here does not move in the simulator. On a real site there is no
  such second copy; in the demo a hand edit makes the drawing and the air
  disagree, and the editor should say so.

## 8e. Rooms that read as rooms (2026-10-13)

Asked for against the EkkoSoft Critical room view: doors, windows, fire
points, and cabinets and plant that look like what they are - without
disturbing the layers.

**The rule that keeps it honest.** Two kinds of thing are drawn and they are
kept apart (`features/floorplan/Fittings.tsx`):

- *From data.* Door openings are the imported fabric's `door` spaces, turned
  into room metres; a door with a BMS contact shows what the contact reads
  (lamp on the lintel, leaf swung open, alarm colour when held). A rack's doors
  hang on the face its `facing` names. A yard has a fence and a roof a
  parapet, because `room_class = 'outdoor'` and the Roof level have no walls.
- *By rule.* Nothing reports where an extinguisher hangs. Fire points are
  drawn where a fire code puts them: a handheld and a manual call point at
  every exit (NFPA 10 / BS 5306 travel distance); the yellow release and blue
  abort stations at the door of a gas-suppressed room and its clean-agent
  cylinder bank against a free wall (NFPA 2001 / EN 15004), in white space
  only. They carry no state, answer no pointer and take no layer colour. The
  "Doors and room fittings" switch turns them off.

**Windows.** A data hall has none - no daylight, no solar gain, no line of
sight (TIA-942, Uptime guidance) - so none is drawn in white space or plant.
The Operations Room, a staffed room, gets a window and a console.

**Cabinets and plant.** Dark trim over the layer's colour, never instead of
it: perforated front door with handle, split rear doors, roof with brush-strip
entries, plinth; "See into racks" takes the front door off. Air handlers get
service doors, a display and a return grille; floor cabinets (UPS, switchgear,
RPP, ATS, MCC) one door section per 0.8 m with a meter window and vent
louvres; cooling towers fans and louvres; water-cooled chillers two
shell-and-tube barrels and a starter panel, not condenser fans; generators
louvres, exhaust stack and silencer; the transformer its bushings and
conservator. The "Cabinet and plant detail" switch turns it off.

**Owed.** A fire panel in the simulator (BACnet/Modbus points for zone alarm,
agent release, VESDA level) would let the fire points carry state and move
them from "by rule" to "from data". Surveyed positions for fittings belong to
Phase 8's drawing import.

## 9. Open questions

- **Q1** Should Phase S2 change the existing cooling numbers? Per-rack supply
  will move room means slightly; alarms and estate compliance may shift. Needs a
  before/after on the live estate.
- **Q2** *Answered 2026-10-08 (§8b B1):* one entry, the floor plan, opening on
  the building; the separate building route is retired.
- **Q3** *Answered 2026-10-13 (§8d):* yes; started with the placement rules and
  endpoints.
- **Q4** Liquid cooling: draw CDU loops and DLC racks (EkkoSense 9.4 added
  these)? Data exists (`cooling` layer, `RoomLiquid.tsx`).

## 10. What we will not build

Live CFD; BIM/IFC import; photoreal vendor model library; robot or fly-through
tours; campus scenery; a composite score; Capture Index presented as measured;
VR/AR.

---

## Appendix A — Files

dcim-platform: `backend/app/models/inventory.py`, `backend/app/importer/simulator.py`,
`backend/app/services/floorplan.py`, `backend/app/services/devices.py`,
`backend/app/repositories/racks.py`, `backend/app/repositories/estate.py`,
new `backend/app/services/twin.py`, `backend/app/api/v1/twin.py`,
`frontend/src/App.tsx`, new `frontend/src/features/twin/*`,
`frontend/src/features/floorplan/FloorPlan.tsx`; §8b adds migration `0102`
(`space`, `room_class='support'`, metric `door_open`, alarm category
`security`), `frontend/src/features/floorplan/Scene.tsx` (`BuildingRung`),
and deletes `frontend/src/features/site/Building.tsx` + `building.css`.

Simulator: `core/hall_geometry.py`, `core/device_manager.py`,
`core/device_state_store.py`, `core/fleet_lifecycle.py`,
`core/topology_engine.py`, `api/routers/topology.py`,
`topologies/dual_dc_enterprise.json`, `docs/heatmap_design.md`,
`tools/build_floorplan_viewer.py` (mesh reference); §8b.3 adds
`core/equipment_geometry.py` (fabric), `core/bacnet_plant_generator.py` (door
BIs, battery AIs), `core/fleet_lifecycle.py` (storage ↔ rack, door events).

## Appendix B — Primary sources

- EkkoSense: https://www.ekkosense.com/faqs/ ·
  https://www.ekkosense.com/ekkosoft-critical/digital-twin/ ·
  https://www.ekkosense.com/resources/tech-tips/taking-advantage-of-zones-of-influence-couldnt-be-easier/ ·
  https://www.ekkosense.com/ekkosoft-critical/cooling-advisor-data-center-cooling-best-practice/ ·
  https://www.ekkosense.com/hardware/
- Huawei NetEco6000 datasheet:
  https://digitalpower.huawei.com/attachments/data-center-facility/69d95f99d691414f8b71c6aaa83097aa.pdf ·
  brochure: https://digitalpower.huawei.com/admin/asset/v1/pro/view/f2595920a9b349ababf4b1100e008698.pdf ·
  temperature map: https://support.huawei.com/enterprise/en/doc/EDOC1100195398/8876262f/optional-viewing-a-temperature-map-on-the-neteco ·
  iCooling: https://www.huawei.com/en/huaweitech/publication/90/smart-cooling-data-centers
- Sunbird 3D + thermal time-lapse: https://sunbirddcim.com/blog/video-dcim-3d-visualization-and-thermal-map
- Siemens WSCO (Vigilent): https://www.siemens.com/en-gb/products/vigilent-white-space-cooling-optimization-wsco/
- Schneider IT Advisor CFD: https://blog.se.com/datacenter/2021/09/21/ecostruxure-it-advisor-cfd-simpler-faster-data-center-cooling-design
- Cadence Reality DC: https://newstaging.cadence.com/en_US/home/resources/product-briefs/cadence-reality-dc-design-pb.html
- NVIDIA PhysicsNeMo DC example: https://docs.nvidia.com/deeplearning/physicsnemo/physicsnemo-core/examples/cfd/datacenter/README.html
- Hyperview 3D: https://www.hyperviewhq.com/blog/3d-data-center-view-and-rack-dashboard-enhancements
- Sensor interpolation patents: https://patents.google.com/patent/US7991592
- Haystack data centers: https://project-haystack.org/doc/docHaystack/DataCenters
- drei instancing performance: https://drei.docs.pmnd.rs/performances/instances
- Timescale hierarchical caggs: https://docs.timescale.com/use-timescale/latest/continuous-aggregates/hierarchical-continuous-aggregates
