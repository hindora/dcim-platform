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

Each phase ends with live verification against the running stack, the
standing rules (CI green, chart/table rules, ship-frontend rule) and the
estate pages agreeing with the twin to the decimal.

**Acceptance for the headline scenario** (Phase 2): trip one CRAH in DC2 Server
Hall A in the simulator → within the NOW horizon the racks nearest that unit go
amber/red in the 3D inlet layer and the mid plane shows a local hot zone, racks
across the hall stay green, RCI_HI drops, the THERMAL page shows the same racks
— and the hall in DC1 does not change.

---

## 9. Open questions

- **Q1** Should Phase S2 change the existing cooling numbers? Per-rack supply
  will move room means slightly; alarms and estate compliance may shift. Needs a
  before/after on the live estate.
- **Q2** Separate TWIN nav entry, or a 2D/3D toggle on the floor plan (NetEco
  pattern)? Recommendation: toggle on the floor plan, plus a site-level TWIN
  entry for the building view.
- **Q3** Is a manual room builder (Phase 6) wanted, or does geometry stay
  import-only? Real deployments need it; the demo does not.
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
`frontend/src/features/floorplan/FloorPlan.tsx`.

Simulator: `core/hall_geometry.py`, `core/device_manager.py`,
`core/device_state_store.py`, `core/fleet_lifecycle.py`,
`core/topology_engine.py`, `api/routers/topology.py`,
`topologies/dual_dc_enterprise.json`, `docs/heatmap_design.md`,
`tools/build_floorplan_viewer.py` (mesh reference).

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
