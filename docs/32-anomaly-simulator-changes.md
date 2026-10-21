# 32 — Anomaly ground truth: simulator changes

Status: **plan. Nothing started.** This document specifies changes to the
**simulator** repo (`Datacenter_Network_Simulator`), not to the platform. It
follows `28` (design) and is a sibling of `29` (data model), `30` (API), `31`
(frontend) and `33` (implementation plan). Doc 33 owns the platform phases,
detector parameters and rollout. This doc owns what the simulator must do so
that the detector can be scored against truth.

Doc 28 states the problem precisely: *the simulator can trip a chiller but
cannot foul one.* Abrupt faults are well covered. Slow faults, sensor faults,
weather, setpoint changes and a durable record of what was injected are not.
Without them, every L3 detector in doc 28's catalogue (CH-2, CH-3, CT-1, CR-3,
CR-4, CD-2 and the rest) would be "tested" against a residual that is flat
forever. That proves nothing.

Conventions follow doc 28. `S:` is the simulator repo and `P:` is
`dcim-platform/`. DSS is `S:core/device_state_store.py`, CM is
`S:core/cooling_model.py`, BPG is `S:core/bacnet_plant_generator.py` and BC is
`S:simulator/bacnet_controller.py`. Every `file:line` below was read for this
doc on 2026-10-21 at simulator HEAD `9516481`, with the working tree dirty in
the files `git status` lists. Line numbers drift, so the task list names
functions as well as lines.

Three kinds of statement are kept apart, as in doc 28:

- **Verified** means read in code at the cited line.
- **[Judgement]** means an engineering choice that no source fixes.
- **[Uncertain]** means it was not verified, or the real-world behaviour depends
  on the vendor.

## 1. Standing constraints

These hold for every change below and are the first thing a review checks.

1. **Physics stays true, the reading lies.** A sensor fault changes what a
   protocol serves. It never changes what the physics reads back. The single
   exception is a fault on a *control* sensor with `affects_control: true`
   (§4.4), where the lie is meant to move the process, because that is what a
   real biased control sensor does.
2. **One identity.** Faults are keyed by device **name** internally and
   addressed by topology **id** on the API. This matches
   `POST /api/bacnet/plant/overrides` (`S:api/routers/bacnet.py:554-575`), where
   keying by IP once caused a documented outage.
3. **Additive API only.** No existing endpoint changes shape. New fields are
   optional, and when absent the old behaviour is unchanged.
4. **Web UI only.** UI work goes in `S:webui/`. Never touch
   `ui/main_window.py`. Load the ui-ux-pro-max and frontend-design skills
   before building. Use `:root` tokens and `NumberInput` for numeric fields. Do
   not run `npm run build`, because `run.sh`/`run.bat` build on launch.
5. **No fire scenario on the live stack unless the user asks for one in that
   message** (`project_fire_system`). Campaign tooling must refuse to run fire
   scenarios without an explicit flag.
6. **Campaign tools are dry-run by default.** A live run needs `--send`. A
   "staging" trap script once fired four CRITICAL UPS events into live
   (`feedback_trap_scripts_need_send_flag`).
7. **The user commits.** Every task ends with a changed working tree and no
   commit. After code changes, run `graphify update .` (simulator `CLAUDE.md`).
8. **Deploy batching.** Build one simulator restart per phase. Never run a
   campaign over a deploy, and never run two at once on the 7.7 GB host
   (`feedback_batch_deploys_tight_host`).

## 2. What the code does today (verified)

### 2.1 The publish pipeline, one tick

`DeviceStateStore._tick` (DSS:6787) runs once per second. The order matters for
where a sensor transform can hook in.

| Step | Where | What it produces | Who reads it |
|---|---|---|---|
| 1. Physics | DSS:6788-6810 (`_compute_cond_loop`, `_compute_chw_penalty`, `_compute_power_flow`, `_compute_chw_loop`), then per device `_step_device` / `_step_ext_state` | `Device` attributes (`inlet_temp`, `cpu_temp`, `humidity`…), `_ext_state_cache` (DSS:493), `_plant_auto_points` (DSS:5184) | Physics, SNMP, Modbus, Redfish |
| 2. SNMP sync | `_sync_snmp` (DSS:8714) → `SNMPRecGenerator.patch_metrics` (`S:core/snmprec_generator.py:830`), every 5 ticks, sharded 100 files per tick | `.snmprec` lines | snmpsim (file-backed) |
| 3. BACnet tick | DSS:6830-6865 → `BACnetController.tick` (BC:654) | BACnet present values, COV | BACnet clients |
| 4. Plant cache | `_publish_plant_state` (DSS:1146-1155) copies `get_telemetry_snapshot()` into `_plant_state_cache` (DSS:477) | `{name: {point: value}}` | **Physics** (next tick) and the SNMP plant OIDs (`snmprec_generator.py:1324-1332`) |
| 5. Modbus | `modbus_controller.tick` (`S:simulator/modbus_controller.py:479-494`) → `apply_ext(_get_ext_state(name))` | Holding registers | Modbus masters |
| 6. Redfish | On demand, `S:simulator/redfish_device.py:450-453` → `rf.thermal(self.device)` etc. (`S:core/redfish_data_generator.py:275-281`) | JSON documents | Redfish clients |

**The plant cache is read back by physics.** This is the most important fact in
this document. `_plant_state_cache` holds what BACnet published, and physics
consumes it on the next tick:

- room supply from running-CRAH `Supply_Air_Temp` (DSS:6548-6555);
- the spatial air model's unit discharge and fan speed (DSS:6654-6668);
- server die temperature from the CDU's `TCS_Supply_Temp` (DSS:7262-7270);
- valve stuck physics from `Position`/`Commanded_Position` (DSS:4784-4795);
- the water tank from tower `Makeup_Flow` (DSS:6282);
- running and alarm status for staging, faults and delivered fraction (DSS:4645-4672, 4746-4752, 6478-6487, 6515-6519).

So a transform applied anywhere before `_publish_plant_state` would feed the lie
into the physics. A CRAH supply sensor reading 3 K high would warm the whole
room model. The transform must split the cache into **truth** (for physics) and
**wire** (for every protocol). §4.3 specifies that split.

### 2.2 Which plant points an operator can actually hold

`BACnetController.tick` merges the operator's map (`plant_alarm_overrides`,
DSS:717) with the store's own per-tick points (`_plant_auto_points`), and the
**auto points win**. The merge copies the operator map and then calls
`.update(auto)` (DSS:6830, 6847-6849). The merged map is then written over the
engine's values (BC:819-821). The engine's values are therefore overwritten
twice: first by the operator, then by the store.

| Plant type | Store-published every tick (operator value is lost) | Holdable (operator value sticks) |
|---|---|---|
| Chiller | `CHW_Supply_Temp`, `CHW_Return_Temp`, **`CHW_Setpoint`**, `CHW_Flow` (DSS:5901-5905); condenser points from `_compute_cond_loop` | `Evap_Pressure`, `Cond_Pressure`, `Run_Hours`, alarm bits |
| CRAH | `Return_Air_Temp`, `Supply_Air_Temp` (running only), `CHW_Valve` (DSS:6119, 6133, 6147) | **`Setpoint`** (published but ignored by physics), `Fan_Speed`, `Supply_Humidity`, `Airflow` |
| CDU | `TCS_Supply_Temp`, `TCS_Return_Temp`, **`TCS_Setpoint`**, `Facility_CHW_Valve`, `TCS_Flow` (DSS:6216-6235) | `Approach_Temp`, `Filter_DP` (both random walks, BPG:143-144), `TCS_Loop_Pressure` |
| Pump | `Flow`, `Speed`, `Diff_Pressure` (DSS:6029-6033) | `Motor_Temp`, `Suction_Pressure` |
| Tower | `Makeup_Flow`, water temperatures, `OA_*` (DSS:4474-4484, 6060-6065) | `Vibration`, `Basin_Level` |
| Valve | — | `Position`, `Commanded_Position` |

Two consequences matter for this plan:

- **Setpoint events do not work today.** `CHW_Setpoint` and `TCS_Setpoint` are
  overwritten from constants every tick (`CHW_SETPOINT_C`, CM:538;
  `_CDU_TCS_SETPOINT_C`, DSS:5668). The CRAH `Setpoint` point holds but physics
  uses the module constant `_SUPPLY_SETPOINT_C = 22.0` (DSS:52, 6096). An
  operator "changing a setpoint" changes a label.
- **Forced-alarm couplings on store-published points are lost.** That is a
  class of bug, not just the CRAH case doc 28 named (§9.2).

### 2.3 Cooling model facts the degradations hook into

| Fact | Where | Consequence for this plan |
|---|---|---|
| Rated COP fixed at 5.5 | CM:56 | Never decays |
| **Published chiller COP is *derived*, not curved**: delivered kW ÷ published kW, after normalisation | DSS:4412-4466 | A COP degradation must raise the **draw**. The published COP then falls by construction and `Q = COP × kW` stays an identity. Overwriting the COP point would break the identity that `feedback_plant_walk_beats_live` says to check. |
| Chiller draw = `chiller_electrical_w(nameplate, plr, city, cond_factor)`, capped at nameplate | DSS:4309; CM:290-305 | Hook for COP and condenser-approach degradation |
| Dry bulb = city mean + seasonal cosine + ±5 K diurnal sine, on the wall clock | CM:106-123 | No override exists |
| Wet bulb = dry bulb − a fixed per-city depression | CM:126-147 | Humidity never changes on its own |
| Tower approach: design 4 K, minimum 1.5 K, set by cells | CM:422-424, 477-486 | Fill fouling is not modelled |
| Condenser supply = max(COND_MIN_C, wet bulb + approach) | CM:503-516; used at DSS:5030 | Hook for tower approach |
| Pump curve h = 1.25·N² − 0.25·Q² | CM:612-645; used at DSS:6029 | Hook for impeller derate |
| CRAH filter: `Filter_Dirty` is a binary, delivered = 0.8 when set | DSS:78, 6478-6487 | No gradual loading. **The CRAH has no `Filter_DP` point at all** (BPG PLANT_SPEC `crah`). |
| CDU `Approach_Temp` and `Filter_DP` are random walks around 3.0 K and 35 kPa | BPG:143-144 | CD-2 and CD-3 would be fitted to noise |
| Chiller has no approach point | BPG PLANT_SPEC `chiller` | CH-3 has nothing to read |

### 2.4 Randomness

- Plant engines are seeded `hash(name) & 0xFFFFFFFF` (BC:311, 452, 535). Python
  salts `str` hashes per process, so the seed changes on every restart.
- The seed only sets **initial offsets** (BPG:321-328). The per-tick walk noise
  uses the **global** `random` module (BPG:465, 517, 618). So even a fixed
  `PYTHONHASHSEED` would not make the walks repeatable, because every thread
  that calls `random` interleaves into the same stream.
- **Per-instrument offsets also use `hash()`** and therefore jump on every
  restart. Examples: room-air probe ±0.6 K and RH ±3 % (DSS:7446-7450), UPS
  battery probe ±0.2 K (DSS:8537), and ASD background (DSS:6316). Also affected
  are `_plant_seed` (`snmprec_generator.py:2476-2480`, a fallback path) and a
  Dell service tag (`S:core/trap_engine.py:1162`).
- **This is a ground-truth defect, not a nicety.** Every simulator restart
  silently injects a step bias of up to 0.6 K on some probes. An L0 bias
  detector or a UP-1 residual would rightly flag it, and the finding would be
  scored as a false positive against a ledger that recorded nothing.
- The store makes 159 calls to the global `random` (count of
  `random.uniform/random/gauss/randint/choice` in DSS).

### 2.5 The DC2 weather bug — latent, and in two places

`city_by_dc` takes the **first** device it sees per DC (DSS:1564-1571), and so
does a second copy of the same logic, `dc_city` in `_compute_power_flow`
(DSS:3661-3674). Doc 28 found only the first. The topology, counted on
2026-10-21:

| (datacenter, datacenter_city) | Devices |
|---|---|
| DC1, Chicago | 388 |
| DC2, New York | 318 |
| **DC2, Chicago** | **10**: `SEN1/SEN2-DC2-HA-R2-01`, `SEN1-DC2-HA-R2-03`, `SEN1/SEN2-DC2-HA-R2-04`, and the same five in HB |

Today the first DC2 device in insertion order is `RTR1-DC2-NR-R1-01`
(New York), so DC2 weather is **currently correct by accident**.
`get_all_devices` returns dict insertion order (`S:core/device_manager.py:2427-2429`).
A topology re-save, a device removal and re-add, or a fleet operation that
reorders nodes would silently move DC2 to Chicago weather. That would shift the
wet bulb by about 3 K seasonally and corrupt every wet-bulb-gated baseline. It
is also a data bug in its own right: those 10 sensors publish
`sysLocation … Chicago … DC2` (`device_manager.py:2127-2136`).

### 2.6 What already exists for injection and campaigns

- Plant overrides: `GET/POST /api/bacnet/plant/overrides` (`bacnet.py:578-615`),
  body `{device, point, value|null}` (`bacnet.py:548-552`).
- Device fault menu: `FAULT_MAP` (`S:api/routers/devices.py:1293`), with
  endpoints `GET /devices/{id}/faults` (`:1444`) and `POST /devices/{id}/fault`
  (`:1659`). It ramps toward a target at a fixed rate per tick: `cpu_high`,
  `temp_high`, `ambient_high`, `humidity_high`, `ups_overload`,
  `pdu_load_high`, plus state pins for ATS, generator, PDU and switchgear. There
  is no CRAH or CDU entry. Those go through plant overrides.
- Metric pins: `POST /devices/{id}/override` (`devices.py:1253`).
- Live harness: `S:tools/_live_api.py` (`connect`, `device_ids`,
  `plant_points`, `force`). Scenarios are built from the live fleet by
  **device type** (`S:tools/live_campaign_exhaustion.py:157-194`). The harness
  refuses to start a scenario until the plant is clean, which means air settled
  as well as water (`is_clean`). It verifies that each injection landed and
  clears everything in a `finally`.
- Offline: `S:tests/test_fault_campaign.py` and the `build_plant` fixture
  (`S:tests/conftest.py:115`, `random.seed(SEED)` at `:137`, `_dt` pinned).
- BACnet objects already carry `status_flags` and `reliability`
  (`S:core/bacnet_object_model.py:200-201`; set only by `Alarm_SensorFault`,
  `S:simulator/bacnet_device.py:321-329`). **The platform's BACnet collector
  ignores both and stamps every sample GOOD** (`P:collector/internal/adapters/bacnet/bacnet.go:347`).
  That is a platform dependency for L0 dropout. See §11.

## 3. Change summary

| # | Change | Contract name | Detector value | Phase |
|---|---|---|---|---|
| C1 | Global seed + stable per-name hash | `DCIM_SIM_SEED` | Repeatable campaigns; removes restart step-biases | S0 |
| C2 | DC2 city fix (data + code) | "fix DC2 city_by_dc weather" | Correct wet bulb for CH-2/CT-1 baselines | S0 |
| C3 | Injection ledger, persisted | `GET /api/faults/ledger` | Event-level scoring (doc 28 §score) | S0 |
| C4 | Publish-layer sensor transform | `POST /api/faults/sensor` | L0; sensor-versus-process `kind` | S0 |
| C5 | CRAH `Alarm_HighTemp` coupling | "verify CRAH Alarm_HighTemp coupling" | CR-1 abrupt ground truth | S0 |
| C6 | Air influence export | `GET /api/air/influence` | Scores the doc 27 D9 fit (L4/L5 zones) | S0 |
| C7 | Degradation factors | `POST /api/faults/degradation` | All L3 slow faults | S1 |
| C8 | Ramp mode on overrides | `plant/overrides` `{ramp}` | Gradual faults on holdable points | S1 |
| C9 | Setpoint events honoured by physics | (via overrides) | CUSUM reset and re-baselining; negative controls | S1 |
| C10 | Weather override | `POST /api/weather/override` | Weather-versus-fault separation; gating | S1 |
| C11 | Web UI | — | Operator access to all of the above | S2 |
| C12 | Campaign library + runner | — | The scored benchmark | S3 |
| C13 | Additive degradation kinds (deviation) | — | Coverage for GN-1, CD-5, UP-2, SV-1/2, TX-1, CR-2, PL-1, VA-2/CR-6 | S4 |

Phase S0 is doc 28's "Phase 0 — ground truth" simulator half. **S1 ships in
the platform's Phase 1 slot, not Phase 3** (doc 33 D20). Phase 1 already
rebuilds the collector for `Status_Flags`, so the mapping rows for the new
BACnet points (`Filter_DP`, `Cond_Approach_Temp`, `Evap_Approach_Temp`) ride on
that one rebuild. The points are then about 5 weeks old when Phase 3 fits its
baselines. S1-8 (setpoints honoured) is also what makes Phase 1's setpoint
negative controls meaningful. S2 (UI) and S3 (campaigns) follow S1 in the same
slot. S4 is optional and runs alongside platform Phase 3.

## 4. Publish-layer sensor transform (C4)

### 4.1 How real sensors fail

The sensors behind the detectors are thermistors and RTDs (supply, return, CHW
and coolant temperatures), capacitive RH elements, magnetic or ultrasonic flow
meters, differential-pressure transmitters (filters, loop ΔP) and valve
position feedback potentiometers. They fail in five standard ways (doc 28 L0;
`device_signatures.md` §7).

| Mode | Real cause | What the BMS/DCIM sees | Realism notes |
|---|---|---|---|
| **Bias** | Calibration offset, a thermowell not fully seated, a probe in the wrong aisle (reads 10–15 K high) | Constant offset against peers and against energy balance | Most common and hardest to see. A rack probe in the hot aisle is the classic case. |
| **Drift** | Element ageing. Capacitive RH drifts about 1–3 % RH per year [uncertain: common vendor range, not verified]. RTD drift is slow. | A slowly growing offset | Indistinguishable from process degradation on a single signal (PolyU, doc 28). Needs redundancy. |
| **Noise** | Loose termination, EMI near VFDs, a failing transmitter | Larger variance, spikes | Common near drives |
| **Stuck** | Frozen transmitter, a controller holding the last good value, an SNMP agent serving a cached reading | Zero variance while coupled variables move | Vendor-dependent. Many controllers hold the last value with no fault flag. |
| **Dropout** | Comms loss, open circuit, power loss to a field device | Missing samples, or a sentinel value, or a fault flag | Varies by protocol. See §4.5. |

**Monitoring sensors versus control sensors.** A rack inlet probe, a PDU probe,
a server BMC inlet, a header thermowell or a UPS battery probe only *reports*.
When it lies, the room does not change. A CRAH's supply-air sensor, a chiller's
leaving-CHW sensor and a CDU's TCS supply sensor are **in the control loop**.
When one of those reads 3 K high, the unit drives the real discharge 3 K
*colder* to bring the reading to setpoint. The reading looks perfect and the
room changes. That is the textbook hidden sensor fault, and it is why doc 28
needs balance and peer redundancy. The simulator models both cases:
`affects_control: false` (the default, the contract's "physics stays true") and
`affects_control: true` (the process moves by −bias) [judgement].

### 4.2 Data structures

New module `S:core/fault_injection.py`. It is pure: no store, no I/O, and the
clock is passed in, so the maths is unit-testable like `air_model.py`.

```python
@dataclass
class SensorFault:
    id: str                 # "flt_<utc yyyymmddThhmmss>_<6 hex>", time-sortable
    device: str             # device NAME (internal key)
    device_id: str          # topology id (API identity)
    point: str              # see §4.6 point namespace
    bias: float = 0.0               # engineering units of the point
    drift_per_h: float = 0.0        # units per hour, added to bias from start
    noise_mult: float = 1.0         # multiplies the point's own noise sigma
    stuck: bool = False             # freeze the WIRE value at activation
    stuck_value: float | None = None # hold this value instead (binaries: proof switch failed)
    dropout_pct: float = 0.0        # 0..100, share of dropout windows
    dropout_window_s: float = 30.0  # length of one window (>= 1 platform poll)
    affects_control: bool = False   # control sensors only (§4.4)
    duration_s: float | None = None # None = until DELETE
    start_ts: float = 0.0           # wall clock, epoch s
    seed: int = 0                   # stable_seed(id) ^ DCIM_SIM_SEED
    # runtime, not API
    held: float | None = None       # stuck value
    rng: random.Random = field(...)

class FaultBook:
    """Thread-safe registry. One instance on DeviceStateStore (`self.faults`).
    API threads write under self._lock; the ticker reads a snapshot taken once
    per tick (never iterate the live dict from the ticker)."""
    def add_sensor(self, f: SensorFault) -> SensorFault
    def remove(self, fault_id: str, reason: str) -> dict | None
    def active(self) -> list[dict]
    def wire_value(self, device: str, point: str, truth: float, now: float,
                   sigma: float) -> float | None    # None = dropped this window
    def apply_bacnet(self, device: str, values: dict, now: float) -> dict  # copy
    def apply_ext(self, device: str, ext: dict, now: float) -> dict        # copy
    def view(self, device: "Device", now: float) -> "DeviceView"            # proxy
    def control_bias(self, device: str, point: str, now: float) -> float   # §4.4
```

The transform is applied in a fixed order: `stuck` first (it returns `held`),
then `truth + bias + drift_per_h × elapsed_h + (noise_mult − 1) × σ × N(0,1)`,
then a clamp to the instrument's span, then dropout gating. σ comes from the
point's own noise: the PLANT_SPEC amplitude for BACnet plant points (BPG:33-225),
and a small per-attribute table for Device attributes (for example `inlet_temp`
0.2 K, which matches U(±0.2) at DSS:7373-7383). The instrument span is
−40…125 °C for a thermistor and 0…100 % for RH, valves and speeds. A drifted
reading cannot leave the transmitter's range [judgement].

`DeviceView` is a read-only proxy over a `Device`. `__getattr__` returns the
transformed value for faulted attributes and delegates everything else. It never
writes to the `Device`, because physics and the rule engine read those same
attributes.

### 4.3 Where it hooks in, and why that keeps physics true

| Protocol | Hook | Change |
|---|---|---|
| **BACnet** | BC `tick`, between the override merge (BC:819-821) and `dev.update_present_values(values)` (BC:836) | `truth = dict(values)`; `wire = faults.apply_bacnet(_nm, values, now)`; keep `self._truth[_nm] = truth`; `update_present_values(wire)`. COV fires on wire changes, so a stuck point produces no COV, which is realistic. A new `get_truth_snapshot()` sits beside `get_telemetry_snapshot()` (wire). |
| **Plant cache** | `_publish_plant_state` (DSS:1146-1155) | `_plant_state_cache` ← **truth** (every physics reader in §2.1 is unchanged). New `_plant_wire_cache` ← wire, and a new accessor `_get_plant_wire_state()` beside `_get_plant_state()` (DSS:531-532). |
| **SNMP, plant OIDs** (CRAH/CDU native cards) | `snmprec_generator.py:1331` | `_get_plant_state` → `_get_plant_wire_state` |
| **SNMP, Device attributes** (servers, sensors, PDU probes) | top of `patch_metrics` (`snmprec_generator.py:830`) | `device = faults.view(device, now)` before any OID is computed |
| **SNMP, ext state** (UPS, genset, ATS, transformer, switchgear) | every `_get_ext_state(device.name)` call inside `patch_metrics` (`:1025, 1049, 1149, 1289, 1318, 1421`) | wrap as `faults.apply_ext(name, _get_ext_state(name), now)` through one helper `_ext_wire(name)` |
| **Modbus** | `modbus_controller.py:494` | `s.apply_ext(faults.apply_ext(s.name, _get_ext_state(s.name), now))` |
| **Redfish** | `redfish_device.py:450-453` route lambdas, and the event/alarm builder (`redfish_data_generator.py:643-655`) | pass `faults.view(self.device, now)`. The BMC's own Health and SEL then react to what its sensor reads, which is correct, because a BMC only knows its sensor. |
| **gNMI** | — | Out of scope. No cooling detector reads network-device telemetry. |

**Why this keeps physics true.** Every physics reader takes its inputs from the
`Device` attributes, `_ext_state_cache` or `_plant_state_cache`. All three stay
truth. Only the protocol renderers see the wire value, and each gets a copy.

**One-tick skew.** SNMP sync runs *before* the BACnet tick in the same `_tick`
(DSS:6820-6866), so the SNMP plant OIDs render the previous tick's wire, which is
1 s old. That is harmless, because snmpsim's effective per-device cadence is
minutes (`feedback_snmp_values_are_file_backed`).

**The device's own alarms see the reading.** A real controller annunciates on
what its sensor reads. Two of the simulator's alarm paths should evaluate on the
wire value for faulted points. The first is the rule engine facts
(`_publish_facts`, DSS:8605), which drive traps. The second is the store-derived
plant alarm bits such as `Alarm_HighReturnAir` at ≥ 42 °C (DSS:6151). Facts
already read `Device` attributes, so passing the `DeviceView` there is a
one-line change. The store-derived plant bits need the wire value of their input
point. **This is in S0 for facts and S1 for plant bits** [judgement]. Until
then, a biased sensor will not trip its own device alarm. That makes the
ground truth conservative, not wrong.

### 4.4 `affects_control` — control sensors

These points can take `affects_control: true`. Any other point returns `422`.

| Device | Point | The controller holds | Process effect |
|---|---|---|---|
| CRAH | `Supply_Air_Temp` | discharge setpoint by CHW valve | true discharge = SP + penalty − bias(t); the valve moves accordingly |
| Chiller | `CHW_Supply_Temp` | leaving CHW setpoint | true CHW supply = SP + penalty − bias(t) (all chillers on the header share it, so apply the mean over running units) |
| CDU | `TCS_Supply_Temp` | TCS setpoint by facility valve | true TCS = SP + … − bias(t) |

Implementation: the store's control blocks subtract
`faults.control_bias(name, point, now)` from the setpoint they control to. These
are the CRAH block (DSS:6096), `_compute_chw_loop` (DSS:5767) and the CDU block
(DSS:6216). The published (wire) value is then truth + bias, which reads on
setpoint. This is the hard case for diagnosis. Rack inlets in the zone fall by
the bias, the reading is perfect, and only peers (RM-4, CR-5) and physical
bounds (rack inlet below CRAH supply minus a tolerance, doc 28 L0) can see it.

### 4.5 Dropout per protocol

A dropout lasts `dropout_window_s` so that it spans at least one platform poll.
A per-tick coin flip would be invisible at 30–120 s polling. Each window is
dropped with probability `dropout_pct / 100`, drawn from the fault's own seeded
RNG.

| Protocol | What a real device does | Simulator rendering |
|---|---|---|
| BACnet | Many controllers hold the last value and set `Reliability` (`NO_SENSOR`, `OPEN_LOOP`, `UNRELIABLE_OTHER`) with the FAULT bit in `Status_Flags`. A field device behind an MS/TP router goes silent instead. | Hold the last wire value, `reliability = 1` (NO_SENSOR), `status_flags |= FAULT`. Restore on window end. |
| Modbus | A register has no quality. Maps carry a validity bit (`project_modbus_plane`). | Hold the register and clear the point's `Data_Valid`/`Reading_Valid` discrete. |
| Redfish | DMTF Sensor: `Reading: null`, `Status.State: "UnavailableOffline"` | Exactly that, via `DeviceView` |
| SNMP | Vendor-dependent: ENTITY-SENSOR `entPhySensorOperStatus = unavailable(2)`, PDU probe state "unavailable", or the agent serves a stale cached value | Where the MIB has a status OID, set it to unavailable and hold the value. Otherwise, hold the value (stale). **[Uncertain]**: per-OID `noSuchInstance` would need snmpsim variation modules, and whether this deployment loads them was not verified. Do not delete `.snmprec` lines or files at runtime (`feedback_dark_device_firewall`). |

**Platform dependency.** The BACnet collector stamps every sample GOOD
(`bacnet.go:347`), so the BACnet dropout rendering is invisible to L0 until the
collector reads `Status_Flags`/`Reliability`. Doc 33 must carry that task. Until
then, BACnet dropout shows up only as a flatline.

### 4.6 Point namespace

`point` is the name the simulator renders from:

- **BACnet plant devices**: the BACnet object name, for example
  `Supply_Air_Temp` (BPG PLANT_SPEC).
- **Servers, sensors and PDUs**: the `Device` attribute, for example
  `inlet_temp`, `cpu_temp`, `humidity`, `fan_speed` or `outlet_temp`.
- **UPS, genset, ATS, transformer and switchgear**: the ext key, for example
  `battery_temp_c` or `gen_fuel_pct`.

A new `GET /api/faults/points?device=<id>` lists the injectable points for a
device. Each entry gives the protocols that render it, whether it is a control
sensor, whether it is store-published (relevant to ramps), and a **platform
metric hint** (`{metric, instance}`). For example, `Supply_Air_Temp` →
`supply_air_temp` and `Filter_DP` → `filter_diff_pressure`, taken from
`P:contracts/mappings/bacnet/objects.yaml`. The hint is copied into the ledger
so that the platform scorer can join without knowing simulator point names. The
table is advisory and duplicated by hand, and a test pins it against the
mapping file when both repos are checked out.

> **Contract deviation (additive).** The contract body has no
> `affects_control`, `dropout_window_s`, `stuck_value` or `campaign` fields, and no
> `GET /api/faults/points`. All are optional and additive. The defaults
> reproduce the contract semantics exactly (`affects_control: false`).

### 4.7 API

All endpoints sit under `/api` with the standard `_AUTH` dependency
(`S:api/main.py:50-75`). They live in the new router `S:api/routers/faults.py`.

**`POST /api/faults/sensor`**

```json
{
  "device": "43ab2687",
  "point": "Supply_Air_Temp",
  "bias": 3.0,
  "drift_per_h": 0.0,
  "noise_mult": 1.0,
  "stuck": false,
  "dropout_pct": 0,
  "duration_s": 21600,
  "affects_control": false,
  "campaign": "crah-supply-bias/r1"
}
```

`201`:

```json
{
  "id": "flt_20261021T101500_7f3a9c",
  "kind": "sensor",
  "device": "CRAH1-DC1-HA-R9-01",
  "device_id": "43ab2687",
  "point": "Supply_Air_Temp",
  "start": "2026-10-21T10:15:00.123Z",
  "end": null,
  "params": {"bias": 3.0, "drift_per_h": 0.0, "noise_mult": 1.0, "stuck": false,
             "dropout_pct": 0, "dropout_window_s": 30, "duration_s": 21600,
             "affects_control": false},
  "protocols": ["bacnet", "snmp"],
  "expected_kind": "sensor",
  "metric_hint": {"metric": "supply_air_temp", "instance": ""}
}
```

| Status | When |
|---|---|
| 404 | unknown device |
| 422 | unknown point for this device; a no-op body (bias = drift = 0, noise_mult = 1, not stuck, dropout 0); `dropout_pct` outside 0–100; `affects_control` on a non-control point |
| 409 | a sensor fault is already active on `(device, point)`. Faults on one point do not stack, so DELETE it first. Stacking would make ground truth ambiguous. |
| 503 | state store not initialised |

`expected_kind` is `sensor`, except that `affects_control: true` records
`sensor` with `process_symptoms: true` in params. Diagnosis is scored on the
sensor.

**`DELETE /api/faults/{id}`** removes a sensor fault or a degradation and
returns `200` with the closed ledger entry. Deleting a degradation stops the
ramp and **does not heal the equipment**. A fouled coil stays fouled until it is
cleaned. To clean it, pass `?restore=true`, which ledgers a separate
`maintenance` entry [judgement: realism. A detector should see the recovery as a
maintenance event, not as the fault ending].

**`GET /api/faults`** lists active sensor faults, degradations, ramps,
setpoint events and the weather override, as
`{"active": [ {id, kind, device, point, start, params, current: {...}} ]}`.
`current` carries the live state, such as the present bias with drift included,
or the present degradation factor.

**`GET /api/faults/{id}`** returns one entry, active or historical
(contract-additive).

## 5. Degradation factors (C7)

### 5.1 How each degrades in a real plant

| Kind | Real mechanism | Real time scale | Real signature | Who exposes it |
|---|---|---|---|---|
| `cop` | Compressor wear, refrigerant undercharge, non-condensables. Condenser approach is not necessarily changed. | Months | kW/ton up 10 %+ at the same load and condenser-water temperature (Fabrico and Oxmaint, vendor rules of thumb, doc 28) | Chiller controller kW, % RLA; COP computed by the BMS |
| `hx_approach` (chiller condenser) | Tube fouling and scale | Months (seasonal) | Condenser approach above the commissioning baseline. Head pressure and lift up. About 1.5 % compressor energy per °F of condensing temperature (Oxmaint, vendor) | Most modern controllers expose saturated condensing temperature or approach. Trane exposes approach directly (`device_signatures.md` §2). |
| `hx_approach` (CDU) | Plate HX fouling, from inhibitor depletion or particulates | Weeks to months | Larger approach at the same heat. The facility valve opens further, then saturates. | CDU controller (Modbus/BACnet/Redfish CoolingUnit) |
| `tower_approach` | Fill fouling and scale, poor distribution, fan or drive problems | Weeks | Approach to wet bulb rising at the same load and wet bulb. Fans reach 100 % earlier. | Tower controller OA points; the BMS computes approach |
| `filter_loading` | Dust loading on CRAH filters or CDU strainers | Weeks (strainers fastest just after commissioning) | ΔP rises monotonically. A constant-airflow EC fan **ramps speed to hold airflow**. Airflow falls only once the fan saturates. Then the "change filter" switch trips. | Analog ΔP on newer units, a ΔP switch on older ones (`device_signatures.md` §1) |
| `pump_derate` | Impeller wear, a clogged strainer | Months | Lower head at the same speed. On ΔP control, speed rises for the same flow and power rises with speed³. | VFD speed and power, loop ΔP |

**The time compression is unrealistic, and it is a choice.** The simulator runs
in real time with no backfill (BPG:328). A real chiller fouls over a season, but
a campaign cannot hold the plant for months. Campaign ramps are compressed 30–100×
(12–72 h). That preserves the *shape*, a monotonic trend under diurnal and load
noise, but not the *rate*. A CUSUM tuned only on compressed ramps will be
over-sensitive to fast drifts and under-tested on realistic ones. **Each L3
detector needs at least one realistic-rate soak**, for example 14 days for
CH-2 and CR-4. Doc 33 should budget the wall-clock for these [judgement].

### 5.2 Data structure

```python
@dataclass
class Degradation:
    id: str
    device: str; device_id: str
    kind: str          # cop | tower_approach | hx_approach | filter_loading | pump_derate
    rate_per_h: float  # magnitude, in the kind's unit per hour (always >= 0)
    target: float      # final value, in the kind's unit
    value: float       # CURRENT value, persisted (§7.3)
    start_ts: float
    reached_ts: float | None = None   # when value hit target (ledgered)
```

The value advances by `rate_per_h × dt / 3600` toward the target on each store
tick, using the store's clamped `dt` (DSS:1263-1290), not the wall clock. A
paused simulator therefore does not foul. One degradation per `(device, kind)`
is allowed. A second POST returns `409`.

| Kind | Unit of `value` | Healthy | Valid device types | Example target |
|---|---|---|---|---|
| `cop` | efficiency multiplier | 1.0 | chiller | 0.80 |
| `tower_approach` | K added to approach | 0.0 | cooling_tower | +3.0 |
| `hx_approach` | K added to the HX approach | 0.0 | chiller (condenser), cdu | +4.0 |
| `filter_loading` | loading fraction (1.0 = the unit's "change filter" ΔP) | 0.0 | crah, cdu | 1.0 |
| `pump_derate` | head multiplier | 1.0 | pump | 0.85 |

### 5.3 Physics hooks

| Kind | Hook | Model [judgement in the coefficients] |
|---|---|---|
| `cop` | Chiller branch at DSS:4309 | `tgt_w = chiller_electrical_w(...) / value`. If that exceeds the nameplate, which is the motor's FLA limit, clip the draw at nameplate and book the unmet share `1 − nameplate/tgt_w` as **capacity loss** into the CHW penalty. A worn machine at full load cannot make the tonnage. The published COP falls by construction (DSS:4412-4466). |
| `hx_approach` (chiller) | Same branch, plus two **new** chiller points | Condenser approach = design (2 K, [judgement]) + value. Draw × (1 + 0.027 × value), which is 1.5 %/°F converted to K [vendor rule of thumb]. `Cond_Pressure` rises with the saturated condensing temperature. New BACnet AIs `Cond_Approach_Temp` and `Evap_Approach_Temp` (Evap stays at design) are **appended** to PLANT_SPEC `chiller`, so that no existing object instance moves (the same discipline as the Modbus validity bit). The plant engine must `continue` out of the walk for any point the store drives (`feedback_plant_walk_beats_live`). |
| `tower_approach` | Condenser supply at DSS:5030 | `base = cond_supply_c(...) + bank_mean(value of running cells)`. The cells share one basin header, so the bank sees the mean. The tower's fan demand rises because the bank must buy approach back (`tower_cell_demand`), so the remaining cells hit 100 % sooner, which is what CT-2 looks for. |
| `hx_approach` (CDU) | CDU block DSS:6195-6222 | Replace the walked `Approach_Temp` with a live value: `approach = 3.0 × (Q/Q_rated)^0.8 + value` [judgement]. The facility valve demand rises with `(_avail_design / (_avail − value))`, so the valve saturates sooner and `_lost` then lifts TCS supply. Exempt `Approach_Temp` from the walk. |
| `filter_loading` (CRAH) | CRAH block DSS:6096-6147, `_crah_delivered_frac` DSS:6465-6487, plus a **new** CRAH AI `Filter_DP` (appended) | `Filter_DP = 60 Pa × (1 + 3 × L)` [judgement: clean about 60 Pa, change at about 250 Pa]. Fan speed for the same airflow rises as √(resistance ratio). Fan power follows speed³ (the existing affinity path). Delivered fraction stays 1.0 until speed would exceed 100 %, then falls as airflow is lost. `Filter_Dirty` is published as 1 once L ≥ 1.0, and that is the **L1 hard alarm the lead time is measured against**. The existing binary `Filter_Dirty` override keeps its step behaviour for backward compatibility. |
| `filter_loading` (CDU) | CDU block | Replace the walked `Filter_DP` with `35 kPa × (1 + 1.5 × L)` and raise `Pump_Speed` for the same `TCS_Flow` as for a pump derate. At L ≥ 1, assert `Alarm_LowFlow` [judgement]. |
| `pump_derate` | Pump head at DSS:6029 | `head = value × pump_head_frac(spd, q)`. The loop holds its ΔP setpoint, so speed is raised until `value × (1.25N² − 0.25Q²)` meets the design head. Clamp at 100 %. Beyond that, head and flow droop. Power follows speed³. |

The new BACnet points (`Cond_Approach_Temp`, `Evap_Approach_Temp`, CRAH
`Filter_DP`) are **BACnet only** in this phase. Adding them to the CRAH SNMP
card would be a generator change, which needs Stop → Regenerate → Start (§8).

> **Contract deviation (dependency).** CH-3 needs `approach_temp{COND}` on
> chillers and CR-4 needs `filter_diff_pressure` on CRAHs. Neither exists on
> the wire today. The platform's mapping
> (`P:contracts/mappings/bacnet/objects.yaml`) needs three rows:
> `Cond_Approach_Temp → approach_temp, instance COND`;
> `Evap_Approach_Temp → approach_temp, instance EVAP`; and CRAH
> `Filter_DP → filter_diff_pressure`. The collector embeds a copy of that file
> (`P:collector/internal/mapping/embedded/bacnet/objects.yaml`), so the Go
> collector must be rebuilt (`feedback_adding_a_metric_key`). The keys already
> exist in the registry, so no new registry key is needed. Docs 29 and 33 must
> carry this. Doc 29 already seeds CR-4 and CH-3 **disabled** for exactly this
> reason (D9, D10). They become enable-able once S1-2/S1-5 and the mapping
> land. Tower approach stays platform-computed from `Cond_Water_Out −
> OA_Wet_Bulb_Temp`, because a BAC tower controller does not normally publish
> approach [judgement].

### 5.4 API

**`POST /api/faults/degradation`**

```json
{"device": "903e3e7f", "kind": "cop", "rate_per_h": 0.01, "target": 0.80,
 "campaign": "chiller-cop-decay/r1"}
```

`201`:

```json
{
  "id": "flt_20261021T110000_02bd11",
  "kind": "degradation",
  "device": "CHL1-DC1-CP", "device_id": "903e3e7f",
  "point": null,
  "start": "2026-10-21T11:00:00.004Z", "end": null,
  "params": {"degradation": "cop", "rate_per_h": 0.01, "target": 0.80, "from": 1.0},
  "expected_kind": "process",
  "eta_target": "2026-10-22T07:00:00Z"
}
```

`422` is returned for: a kind not valid for the device type; `rate_per_h ≤ 0`;
a target outside the kind's physical band (cop 0.5–1.0, approach 0–10 K,
loading 0–1.5, derate 0.5–1.0). `409` is returned when the device already has
an active degradation of that kind.

> **Contract note.** The contract lists only `{device, kind, rate_per_h, target}`.
> `campaign` is additive. `point` is null for degradations in the ledger,
> because the fault is in the machine, not in a point.

## 6. Ramp overrides, setpoint events and weather (C8–C10)

### 6.1 Ramp mode on plant overrides

**Real world.** Operators rarely ramp anything by hand. Ramp mode is a
*generic test instrument* for gradual faults on points that nothing else drives:
bearing temperature creep (`Motor_Temp`), tower vibration growth (`Vibration`),
basin draw-down (`Basin_Level`), slow loop-pressure decay (`TCS_Loop_Pressure`).
Each is honest only to the extent that the point is *not* coupled to physics.
The ledger records it as `kind: ramp` with `expected_kind: process`, and §10
says where coupling is missing.

`PlantOverride` (`bacnet.py:548-552`) gains two optional fields:

```json
{"device": "893e478e", "point": "Motor_Temp", "value": null,
 "ramp": {"rate_per_s": 0.000139, "target": 62.0}}
```

- `value(t) = start + rate_per_s × t`, moving toward the target and holding
  there. `start` is `value` if it is given, otherwise the point's current
  **truth** value.
- The store keeps `plant_ramps[name][point]`. It evaluates them once per tick
  *before* the merge at DSS:6830 and writes the result into the operator map.
  The existing pipeline is therefore unchanged.
- **Refusals.** `409` for a point in the store-published set (§2.2), with
  `detail` naming the right tool: `POST /api/faults/sensor` for a lying
  reading, or `POST /api/faults/degradation` for a real process change. A ramp
  there would be overwritten every tick and would ledger a fault that never
  reached the wire, which is the exact silent failure BC:822-890 was added to
  catch. `422` for a binary point.
- Setpoint points are the exception (§6.2): they are rampable once C9 lands.
- `value: null` with no `ramp` clears it, as today, and closes the ledger entry.
- Optional `oscillate: {"period_s": 240, "amplitude": 15}` adds a sine on top,
  for valve hunting (VA-2) on the 4 valve devices.

> **Contract deviation (additive).** `oscillate` is not in the contract. It is
> the only way to give VA-2 ground truth without a new physics mode. It is
> realistic only for the stand-alone valves, whose position is not
> store-published.

### 6.2 Setpoint events (C9)

**Real world.** Setpoint changes are routine, deliberate and logged by the BMS.
Common examples are a CRAH discharge raised from 22 to 24 °C for energy, a CHW
reset from 7 to 9 °C to gain economiser hours, or a CDU TCS setpoint moved for
a new GPU generation. They are exactly what an L3 baseline must *not* call a
fault, and doc 28 lists them as the test for CUSUM reset and re-baselining.

**Design.** Physics reads the setpoint per device instead of the module
constants.

| Setpoint | Today | Change |
|---|---|---|
| CRAH `Setpoint` | holdable, physics uses `_SUPPLY_SETPOINT_C` (DSS:52, 6096, 6564, 6660) | `_crah_sp(name)` = operator override if present, else 22.0. Used for `_sa`. Also the stopped/fallback paths at 6564 and 6660 use the room mean of the units' setpoints. |
| Chiller `CHW_Setpoint` | **store-published** from `CHW_SETPOINT_C` (DSS:5904) | `_chw_sp(dc)` = the operator override on any chiller in the DC. All chillers share one header, so the API applies it to every chiller of the DC and returns the list. Used at 5767, 5904, 5915-5920, 6174 and 6208-6210. |
| CDU `TCS_Setpoint` | **store-published** from `_CDU_TCS_SETPOINT_C` (DSS:6222) | `_tcs_sp(name)`, per CDU. Used at 6208-6222. |

A setpoint override, with or without a ramp, ledgers as `kind: setpoint` with
params `{from, to, rate_per_s}` and `expected_kind: none`. It is a context
event, not a fault. A CHW reset also changes chiller efficiency in reality, by
about 2–3 % per K of lift [uncertain: rule of thumb]. The current kW curve has
no lift term (CM:241-305). **Recommend adding `1 − 0.025 × (sp − 7.0)` to the
chiller draw**, so that a CHW reset moves COP the right way and a two-sided
CH-2 CUSUM is tested against a legitimate improvement.

### 6.3 Weather override (C10)

**Real world.** Weather is the largest legitimate driver of plant efficiency.
Wet bulb sets condenser water and therefore lift. A heatwave lowers COP and
raises tower approach demand with no fault at all. Real sites take OA from a
rooftop station (BACnet/Modbus) or from the tower controller. CH-2 and CT-1
regress on wet bulb precisely so that this is not flagged.

**Design.** `S:core/cooling_model.py` gains a module-level override table,
consulted by `ambient_c` (CM:106-123) and `wet_bulb_c` (CM:143-147):

```python
_WEATHER: dict[str, tuple[float, float, float]] = {}   # city_key -> (dry_off, wet_off, until_ts)
def set_weather_override(city: str | None, dry_c: float, wet_c: float, until_ts: float) -> None
def clear_weather_override(city: str | None) -> None
```

- `ambient_c' = ambient_c + dry_off`.
- `wet_bulb_c' = ambient_c' − depression + wet_off`, clamped to `≤ ambient_c'`.
  A dry-bulb offset alone therefore moves both temperatures, keeping the
  existing constant-depression model. A wet-bulb offset alone is a humidity
  change. Wet bulb can never exceed dry bulb.
- The override expires at `until` (the `now` argument is honoured, so tests
  are deterministic). Expiry is ledgered as `end_reason: expired`.
- Everything that already reads these functions follows automatically: chiller
  kW through `ambient_factor` (CM:150-157), the economiser, condenser supply
  (CM:503-516) and the tower `OA_*` points (DSS:4474-4484). Whether the
  transformer oil model or anything else reads ambient was **not verified**.

**API**

```json
POST /api/weather/override
{"datacenter": "DC1", "dry_bulb_offset_c": 8.0, "wet_bulb_offset_c": 6.0,
 "until": "2026-10-22T06:00:00Z", "campaign": "heatwave/r1"}
```

`201` returns `{id, kind: "weather", city: "chicago", start, until, params}`.
`DELETE /api/weather/override?datacenter=DC1` clears it early.
`GET /api/weather` returns the live dry and wet bulb per DC with any override
in force.

> **Contract deviation.** The contract body has no scope field. A weather
> override must name a site, because the two DCs share no sky. `datacenter` is
> optional and defaults to **all** sites, which keeps the contract body valid.
> `DELETE` and `GET /api/weather` are additive. The override resolves
> `datacenter` through the fixed city map (C2), so C2 must land first.

## 7. Injection ledger (C3)

### 7.1 Scope: every injection, from every channel

Event-level scoring (doc 28) counts a finding as false when "it overlaps no
injection". An injection made through an *old* endpoint, but missing from the
ledger, would therefore score a correct detection as a false alarm. So the
ledger records every channel, not just the new router.

| Channel | `kind` | `expected_kind` |
|---|---|---|
| `POST /api/faults/sensor` | `sensor` | sensor |
| `POST /api/faults/degradation` | `degradation` | process |
| `POST /api/bacnet/plant/overrides` (value) | `override` | process. A `Position` hold on a valve **without** `Alarm_ActuatorFault` is `sensor`: physics only restricts flow when the bit is set (DSS:4784), so a held position alone is a lying feedback sensor. |
| `… /plant/overrides` with `ramp` / `oscillate` | `ramp` | process |
| setpoint points via overrides | `setpoint` | none (context) |
| `POST /api/devices/{id}/fault` | `fault_menu` | process for load and state faults; `sensor` for `ambient_high`/`humidity_high` on a sensor device [judgement: the probe reads high, the room does not change] |
| `POST /api/devices/{id}/override` | `pin` | per metric, from a table |
| `POST /api/bacnet/electrical/utility`, `/ats` | `electrical` | process |
| `POST /api/modbus/fire/smoke`, `/abort`, `/reset` | `fire` | none (context: must suppress) |
| `POST /api/topology/links/break`, `/restore` | `link` | none |
| `POST /api/redfish/action` | `power` | process |
| `POST /api/weather/override` | `weather` | none (context) |
| `POST /api/tick/settings` (pause), fleet `advance`/`start`, simulator and protocol start/stop, dataset regeneration, and a deploy that changes noise or offsets (C1) | `context` | none |

### 7.2 Record format

The ledger is event-sourced JSONL, one line per event, appended and never
rewritten:

```json
{"v":1,"op":"open","id":"flt_20261021T101500_7f3a9c","kind":"sensor",
 "device":"CRAH1-DC1-HA-R9-01","device_id":"43ab2687","point":"Supply_Air_Temp",
 "start":"2026-10-21T10:15:00.123Z","params":{"bias":3.0,"duration_s":21600},
 "expected_kind":"sensor","metric_hint":{"metric":"supply_air_temp","instance":""},
 "source":"POST /api/faults/sensor","campaign":"crah-supply-bias/r1",
 "sim":{"seed":42,"air_mode":"spatial","head":"9516481"}}
{"v":1,"op":"close","id":"flt_20261021T101500_7f3a9c",
 "end":"2026-10-21T16:15:00.140Z","end_reason":"expired"}
```

`end_reason` is one of `deleted | expired | target_reached | cleared |
superseded | sim_restart | restore`. The degradation `target_reached` event is
an `op: "mark"` line. The degradation stays open after reaching its target,
because a fouled coil does not unfoul.

Times are UTC ISO-8601 with milliseconds, from the same wall clock the
simulator's protocols stamp. The platform aligns findings by the collector's
sample timestamps, so clock skew between the hosts must be under one poll
interval. The WSL stack and the Windows host share a clock, which is acceptable.

### 7.3 Persistence and restart

| File | Content | Write discipline |
|---|---|---|
| `S:datasets/faults/ledger.jsonl` | The ledger | Append, `flush()` + `os.fsync()` per line. The rate is a few lines per hour, so the cost is nil. |
| `S:datasets/faults/active.json` | Active sensor faults and degradations with their **current values** | tmp + `os.replace`, every 300 ticks and on stop, the same pattern as `ev2_energy.json` (BC:341-372) |

- `datasets/` is gitignored, so neither file is committed. They live outside
  `datasets/snmp/`: snmpsim serves every file in that directory, and the orphan
  reaper deletes them (`project_dataset_binding_reconcile`).
- **On start:** degradations and sensor faults are **resumed** from
  `active.json` with their persisted value. A fouled condenser is still fouled
  after a BMS reboot. Each resume writes `op: "resume"` with the gap
  `[last_save, now]`. A sensor fault whose `duration_s` expired during the
  downtime is closed at its expiry time.
- **Plant overrides, ramps, setpoints and weather are in-memory today** and are
  lost on restart (`plant_alarm_overrides` at DSS:717). On start, every ledger
  entry still open for those kinds is closed with `end_reason: sim_restart` at
  the last `active.json` save time. Persisting overrides is out of scope. It
  would change existing restart behaviour that the live campaigns rely on.
- **A corrupt last line** (a crash mid-write) is skipped with one warning. The
  fold tolerates `close` without `open`.
- **Rotation:** none in v1. At about 10 kB per day it is not needed for years.

### 7.4 API

`GET /api/faults/ledger?since=&until=&kind=&device=&campaign=&limit=500&cursor=`
folds the events into entries. The fields are exactly the contract's
`{id, kind, device, point, start, end, params}`, plus the additive `device_id`,
`expected_kind`, `metric_hint`, `source`, `campaign`, `end_reason`, `marks[]`,
`resumes[]` and `sim`. Results are newest first and paginated by an opaque
cursor (the line offset).

```json
{"entries":[{"id":"flt_…","kind":"degradation","device":"CHL1-DC1-CP",
  "device_id":"903e3e7f","point":null,"start":"…","end":null,
  "params":{"degradation":"cop","rate_per_h":0.01,"target":0.8,"from":1.0},
  "expected_kind":"process","marks":[{"at":"…","what":"target_reached"}]}],
 "next_cursor":"18234"}
```

> **Contract note.** The contract says the ledger holds "every injection ever".
> This doc reads that as every channel (§7.1), not only `/api/faults/*`. That
> is wider than a literal reading of the contract, and it is required for
> honest scoring.

## 8. Restart, regeneration and protocol implications

| Change | BACnet restart? | SNMP Stop → Regenerate → Start? | Topology fingerprint changes? | Platform action |
|---|---|---|---|---|
| C1 seed + stable hash | Restart (seeds) | No | No | **One-time step** in walk offsets and per-instrument offsets. Ledger it as `context: sim_change`, so the platform excludes that window from baselines. |
| C2 DC2 city (data) | — | **Yes** | **Yes**: `sys_location` is hashed (`S:core/dataset_fingerprint.py:50`) and contains the city | Re-import, or let discovery refresh the 10 sensors' location |
| C2 (code: majority vote) | Restart | No | No | None |
| C3 ledger | Restart | No | No | Harness reads it |
| C4 sensor transform | Restart | No: runtime patch only | No: values are excluded from the fingerprint | Collector must read BACnet `Status_Flags` (§4.5) |
| C5 HighTemp | Restart | No | No | None |
| C6 influence | Restart | No | No | Harness reads it |
| C7 new BACnet points | **BACnet Stop → Start**, because objects are built from PLANT_SPEC at start | No (BACnet only) | No | Mapping rows + collector rebuild (§5.3). Re-discover objects. |
| C8–C10 | Restart | No | No | None |

Operational traps, from memory:

- **A headless restart starts only the REST API.** SNMP, BACnet, Redfish, gNMI
  and Modbus must be started again from the web UI or an authenticated call.
  Verify with `pgrep -af snmpsim` and `ss -lunp | grep :161`
  (`feedback_sim_restart_drops_protocols`). Starting SNMP without
  `{"port": 1611}` moves it off the user's port.
- **The restart must carry `DCIM_SIM_AIR_MODEL=spatial`**. Without it, C6
  returns 409 and every zone campaign is void (DSS:878-879; `project_digital_twin_plan`).
- **A generator change is not picked up by restart alone.** The fingerprint
  hashes the topology, not the generator (`project_dataset_binding_reconcile`).
  Never use `/snmp/reload`.
- `DCIM_SIM_SEED` goes in `auth.env`, which `run.sh` already exports.

## 9. Bug fixes

### 9.1 DC2 weather (C2)

1. **Data.** Add a new `S:tools/fix_dc_city_labels.py`, dry-run by default and
   idempotent, the convention of `rederive_power_sources.py`. It sets
   `datacenter_city` to the DC's majority city for any device that disagrees,
   prints the 10 it changes, and writes the topology. Then run
   Stop → Regenerate → Start.
2. **Code.** Add one helper, `_dc_city_map(devices) -> {dc: city}`, using a
   **majority vote** and logging a warning listing the dissenters once per
   change. Use it at both DSS:1564-1571 and DSS:3661-3674. Two copies of a
   derivation is how one stayed buggy.
3. **Test.** `tests/test_dc_city.py` checks that a topology with the first DC2
   device labelled Chicago still yields New York, and that the shipped topology
   has zero dissenters.

### 9.2 CRAH `Alarm_HighTemp` coupling (C5), and the class it belongs to

**Verified from code, not live.** The engine applies +6 K to `Supply_Air_Temp`
for a forced `Alarm_HighTemp` (BPG:754-756). For a running CRAH, the store
publishes `Supply_Air_Temp` every tick (DSS:6133), and the merge lets that
overwrite the engine (DSS:6847-6849, then BC:819-821). The +6 K never reaches
the wire, and the room never warms, because the room model reads the published
point (DSS:6548-6555). The forced bit is the only tell. That is the failure
mode the simulator's own comments call "the signature of the campaign rows that
came back with pre and during identical" (BC:822-835).

**The same mechanism defeats every forced-alarm coupling whose target point is
store-published.** From BPG:668-778 against §2.2:

| Alarm | Lost coupling |
|---|---|
| CRAH `Alarm_HighTemp` | Supply +6, Return +5, CHW_Valve +30 |
| CRAH `Alarm_AirflowLoss` | Supply +4, Return +6 |
| CRAH `Alarm_HighReturnAir` | Return +8, CHW_Valve +25 |
| CRAH `Filter_Dirty` | Return +3, CHW_Valve +12 |
| CDU `Alarm_HighSupplyTemp` | TCS_Supply +6, Facility_CHW_Valve +25 |
| CDU `Alarm_PumpFault` / `Alarm_LowFlow` | TCS_Flow, TCS_Supply |
| Chiller `Alarm_HighCHWSupply` | CHW_Supply +3, CHW_Return +3 |

Not every one of these needs fixing. Some are already represented in physics.
For example, AirflowLoss sets delivered to 0, and the room warms through that
path. **Fix CRAH `Alarm_HighTemp` in S0**, because CR-1 ground truth depends on
it. Do it store-side in the CRAH block: for a running CRAH with the bit forced,
`_sa += 6.0 × s(t)` using the engine's severity ramp (0.25 → 1.0 over about
45 s), and drive `CHW_Valve` to 100. It means "the coil is starving", which is
the CHW-starved branch of CR-1. Because the room model reads the published
discharge, the hall warms. **Test:** settle 200 ticks, then assert that the
published supply is at least SP + 5.5 K, the room mean supply rose by about
6/7 K, and clearing restores the previous state. **Fix CDU
`Alarm_HighSupplyTemp` the same way in S1.** For the rest, add a log line on
the first tick that a forced coupling targets a store-published point, so the
gap is visible rather than silent.

### 9.3 Seed and stable hashing (C1)

- New `S:core/sim_seed.py`:
  - `GLOBAL_SEED = int(os.environ.get("DCIM_SIM_SEED", "0"))`;
  - `stable_hash(s) = zlib.crc32(s.encode())`;
  - `stable_seed(name) = stable_hash(name) ^ GLOBAL_SEED`.
- `S:app/main.py`: call `random.seed(GLOBAL_SEED)` once, before the stores are
  built, and only when `DCIM_SIM_SEED` is set. Unset keeps today's behaviour.
- BC:311, 452, 535: `seed=stable_seed(name)`.
- BPG: the engine keeps `self._rng = random.Random(seed)` and uses it for the
  walk noise at BPG:465, 517 and 618. Each device's noise sequence is then
  independent of thread interleaving.
- **Per-instrument offsets use `stable_hash(name)` without the global seed**:
  DSS:6316, 7446, 7450, 8537, `snmprec_generator.py:2480` and
  `trap_engine.py:1162`. A real instrument's offset is a property of the
  instrument. It must survive restarts *and* seed changes.
- **This makes `PYTHONHASHSEED` unnecessary.** Doc 28 suggested pinning it.
  Stable hashing is more robust, because it does not depend on a launch script
  being used.

**Honest limit.** A live run is still not bit-reproducible. The ambient model
reads the wall clock (CM:115-118), `dt` jitters, pollers sample at their own
phase, and the store makes 159 calls to the global `random` while other threads
(fleet, API) share that generator. The seed makes campaigns **statistically
repeatable**: the same per-device noise processes and the same offsets.
Bit-exact replay is available only offline, with `build_plant` and a pinned
`_dt`. Migrating the store to an owned `self._rng` is optional follow-up, not S0.

> **Contract note.** The contract names only `DCIM_SIM_SEED`. Doc 28's
> `PYTHONHASHSEED` is superseded by stable hashing. Nothing in the platform
> depends on it.

## 10. Air influence export (C6)

**Real world.** No real site has this. Operators estimate CRAH zones from CFD,
from commissioning failover tests or by fitting (EkkoSense "Zones of
Influence", a vendor claim). The simulator can export the *actual* weights its
spatial model used, which is a rare luxury: the platform's fitted matrix (doc 27
D9, doc 28 L4/L5) can be scored against truth.

**What the model uses** (`S:core/air_model.py`):

- `w_ij = delivered_j × exp(−d_ij / 5 m)` (`:128-134`, `INFLUENCE_LENGTH_M = 5.0` at `:35`);
- tile air = Σ_j w_ij · (supply_j + 0.10 K/m · d_ij) / Σ_j w_ij (`:174-177`);
- inlet = tile + recirc × (hot − tile), with recirc from the containment and
  open-U tables plus the starved share (`:149-157`);
- return_j uses geometric weights × heat, mixed 0.85/0.15 with discharge
  (`:192-202`).

**Design.**

- `air_model.py` gains a pure function,
  `influence_matrix(racks, units, length_m) -> {rack_id: {unit_id: (w_live_norm, w_geo_norm)}}`,
  which normalises per rack.
- `_solve_room_air` (DSS:6646-6698) stashes its `(racks, units, containment)`
  per room in `self._air_inputs[rk]`, beside `_air_rooms` (DSS:881, 6631-6644).
  It holds a reference, not a copy, and replaces it every tick.

`GET /api/air/influence?datacenter=DC1&room=HA`:

```json
{
  "mode": "spatial", "tick": 128734, "at": "2026-10-21T12:00:01Z",
  "length_m": 5.0, "plenum_gain_k_per_m": 0.10,
  "rooms": [{
    "datacenter": "DC1", "room": "HA", "containment": "cold_aisle",
    "units": [{"name": "CRAH1-DC1-HA-R9-01", "x": 3.1, "y": 18.4,
               "delivered": 1.0, "fan_frac": 0.62, "supply_c": 22.0}],
    "racks": [{"rack": "2/01", "x": 6.0, "y": 4.6,
               "devices": ["SRV01-DC1-HA-R2-01", "SEN1-DC1-HA-R2-01", "PDUA-DC1-HA-R2-01"],
               "recirc_mid": 0.04, "starve": 0.0,
               "weights": {"CRAH1-DC1-HA-R9-01": {"live": 0.21, "geometric": 0.21,
                                                  "d_inlet_d_supply": 0.20}}}]
  }]
}
```

- `live` is the tile-mix share now. `geometric` is the share if every unit were
  delivering. `d_inlet_d_supply = (1 − recirc_mid) × live` is the
  quantity a platform regression of rack inlet on CRAH supply actually
  estimates, so score the fit against it.
- Racks are identified by `row/num` within (dc, room). `devices` lists the
  names in the rack, which the platform already holds. The device names in the
  example above are illustrative.
- `409` with `{"detail": "air model is 'uniform' — no influence structure exists"}`
  is returned in uniform mode. Shadow mode returns the matrix with
  `"published": false`, because the computed field is not what inlets show.

## 11. Interaction with the platform's protocols

| Platform reads | Protocol and cadence (doc 28) | Sensor fault visible? | Degradation visible? | Notes |
|---|---|---|---|---|
| Plant (chiller, pump, tower, valve, CRAH, CDU) | BACnet, 30 s | Yes, on the next tick | Yes | BACnet dropout is invisible until the collector reads `Status_Flags`. **Platform task.** |
| CRAH, CDU | SNMP native card, plus BACnet | Yes (wire cache) | Yes, for existing OIDs | New points are BACnet only |
| Servers | Redfish 60 s + SSE push; SNMP 120 s | Yes, both, consistently | n/a | The cross-protocol check (BMC inlet versus PDU probe) works because both read the same `DeviceView` |
| Sensors, PDU probes | SNMP 120 s | Yes | n/a | snmpsim serialises about 6 polls/s fleet-wide, so the *effective* per-device cadence can be tens of minutes (`feedback_snmp_values_are_file_backed`). L0 windows on SNMP must allow for that. |
| UPS, genset, ATS, switchgear | Modbus 30 s and SNMP | Yes, both, because both render `apply_ext` | n/a | Modbus dropout clears the validity bit |
| Network | gNMI / SNMP | No | No | Out of scope |

**Collector-side quality.** A biased or drifting reading is GOOD on the wire,
which is correct: real sensors do not flag their own bias. A stuck reading is
GOOD, also correct. Only dropout carries a quality signal. On SNMP and Modbus,
the platform's quality mapping for "unavailable" states was not verified
[uncertain]. Doc 33 should check it.

## 12. Web UI (C11) — simulator web UI only

Before building, load the ui-ux-pro-max and frontend-design skills
(`feedback_ui_skills_required`). Use `:root` theme tokens only
(`project_webui_theme_tokens`) and `NumberInput` for every numeric field
(`feedback_number_input`). Never touch `ui/main_window.py`. Do not run the
build.

| Surface | File | Content |
|---|---|---|
| **Faults** tab, below the protocol divider (`PROTOCOL_TABS` membership, `S:webui/src/components/RightPanel/index.tsx`) | new `RightPanel/FaultsPanel.tsx` | (1) **Active**: one row per active fault, ramp, setpoint and weather, showing kind, device, point, live value (current bias or factor), age and a Clear button. Degradations offer "Stop" and "Restore (maintenance)". (2) **Weather**: per-DC dry and wet offsets with an until picker and live OA. (3) **Ledger**: newest first, filter by kind and campaign, and a "copy as JSON" action. (4) **Seed + air mode** read-out, with a warning chip when the air mode is not `spatial`. |
| Node context menu, "Simulate Fault" (`S:webui/src/components/TopologyCanvas/NodeContextMenu.tsx:874-946`) | same file + new `SensorFaultDialog.tsx`, `DegradeDialog.tsx` | **Sensor fault…**: the point picker from `GET /api/faults/points`, with bias, drift, noise, stuck, dropout and duration fields. `affects_control` appears only for control points. **Degrade…**: only the kinds valid for the type, with rate, target and a computed ETA. The device's active faults are listed with an ACTIVE marker, like plant overrides today (`:730`, `:811`). |
| Plant override UI | wherever `setPlantOverride` is driven | Add an optional ramp (rate, target) for holdable points. Grey it out, with a tooltip naming the right tool, for store-published points. |
| API client | `S:webui/src/api/client.ts` (beside `:259-261`) | `faultsActive`, `faultPoints`, `addSensorFault`, `addDegradation`, `deleteFault`, `ledger`, `weather`, `setWeather`, `clearWeather`, `airInfluence` |

## 13. Fault-campaign library (C12)

### 13.1 Mechanics

- Add `S:tools/anomaly_campaigns.json`, a data file that is the single source
  for the scenario list. Doc 33's platform harness reads the same file, so the
  expectations cannot drift between repos.
- Add `S:tools/live_campaign_anomaly.py`, built on `_live_api.py`. It reuses
  `is_clean`/`wait_clean` from `live_campaign_exhaustion.py` and resolves
  devices by **type**, never by hard-coded names. It tags every injection with
  `campaign: "<scenario>/<run_id>"`, verifies each landed by reading back the
  wire *and* the truth (`GET /api/bacnet/plant/metrics?view=truth|wire`, an
  additive query param), and clears everything in a `finally`.
- It needs `--send` to touch the live simulator. Without the flag it prints the
  plan. Fire needs `--allow-fire` as well. It refuses to start unless
  `GET /api/air/influence` answers (spatial mode).
- Durations are listed as **fast / realistic** where they differ (§5.1). Lead
  time is detector onset relative to the first L1 hard alarm on the same chain.
  "No L1" means no hard alarm ever fires, which is the anomaly tier's whole
  case.
- **Windows are design targets derived from simulator constants, not
  measurements.** These constants are: CHW penalty τ ≈ 16 s and runaway
  0.20 K/s (DSS:4811-4818); `Alarm_HighCHWSupply` at +2 K for 180 s
  (DSS:5915-5923); condenser rise 0.35 °C/s × loss and trip at 42 °C after 5 s
  (DSS:5112-5212); stopped-CRAH soak τ 240 s; server die τ 150 s. Add to them
  the poll cadence and the doc 28 dwell. The first live run of each scenario
  calibrates its window, and that calibration is an acceptance item (§14).
- L3 scenarios assume fitted baselines (2–4 weeks of clean history, doc 28
  Phase 3). The ledger windows feed `anomaly_baseline_exclusion` (doc 29), so
  the next nightly fit does not learn the fault.
- **L0 IDs follow doc 29 §5 D1**: DQ-1 dropout, DQ-2 non-GOOD run, DQ-3
  physical range and sentinels, DQ-4 spike, DQ-5 flatline while a coupled
  variable moves, and DQ-6 bias/drift. DQ-1 to DQ-5 are seeded in 0112.
  **DQ-6 needs baselines and ships in 0113**, so a scenario that expects DQ-6
  scores only once Phase 3 is live. Before that, the same scenario scores
  whichever of CR-5, RM-4 and DQ-5 it lists.
- **Seeded state (doc 29 §5, §12).** Some detectors are seeded disabled or not
  seeded at all. Scenarios that depend on them are marked in the tables below:
  - **pending point availability**: CR-4 (no CRAH filter ΔP, D9) and CH-3 (no
    chiller approach point, D10). This simulator *can* publish both. C7 / S1-2
    adds `Cond_Approach_Temp`/`Evap_Approach_Temp`, and S1-5 adds CRAH
    `Filter_DP`. They then need the platform half of the metric-key chain
    (`feedback_adding_a_metric_key`). The registry keys `approach_temp` and
    `filter_diff_pressure` already exist, so no new registry key is needed.
    What is needed is mapping rows in `P:contracts/mappings/bacnet/objects.yaml`
    plus its embedded collector copy, a Go collector rebuild, and BACnet object
    re-discovery. After that, the detector rows are enabled. Docs 29 and 33
    own those steps.
  - **pending a loop entity**: PL-2 is not seeded (D11).
  - **seeded disabled; enable for the run**: PL-1 (D11), CT-2, CT-3 and AT-1.
    The runner PATCHes `/api/v1/anomalies/detectors/{id}` `enabled: true` for
    the scenario and restores the previous value afterwards, and the ledger
    records it as `context`.
  - **VA-1 is valve-only** (D7). VA-1 scenarios target the 4 `valve` devices,
    never CRAHs. CR-6 (CRAH hunting) needs only one position and still applies.
  - **PU-3 is proof-versus-process** (D8), not command-versus-proving. See
    `pump-proof-lost` below.

### 13.1a `anomaly_campaigns.json` format

There is one file and two readers: the simulator runner (S3-1) and the
platform scorer (doc 33 §4, `P:backend/app/analytics/evaluation.py`). The
fields below are the ones doc 33's harness consumes: expected detectors,
`kind`, calibrated windows, expected misses, `must_not` sets and
enable-for-run detectors.

```json
{
  "version": 1,
  "scenarios": [{
    "name": "crah-filter-loading",
    "tier": "gradual",                     // abrupt | gradual | sensor | context
    "requires": {"air_mode": "spatial", "sim_tasks": ["S1-5"],
                 "allow_fire": false, "fleet_paused": true},
    "pending": "point_availability",       // null | point_availability | loop_entity | s4
    "runs": 1,                             // abrupt 3 (3-of-3 recall gate), gradual 1
    "offline_twin": "tests/test_fault_campaign.py::test_crah_filter_loading",
    "injections": [{
      "api": "POST /api/faults/degradation",
      "target": {"device_type": "crah", "room": "HA", "pick": "first"},
      "body": {"kind": "filter_loading", "rate_per_h": 0.02, "target": 1.0},
      "hold_s": 180000
    }],
    "expected": [
      {"detector": "CR-4", "kind": "process", "root": "injected_device",
       "window_s": {"min": 21600, "max": 72000, "calibrated": false}},
      {"detector": "CR-3", "kind": "process", "root": "injected_device",
       "window_s": {"min": 36000, "max": 108000, "calibrated": false}}
    ],
    "l1": {"signal": "alarm_state{Filter_Dirty}", "expect_lead_s": {"min": 72000}},
    "expected_misses": [],
    "must_not": ["CR-1", "CR-2"],
    "enable_for_run": [],
    "incident": null                       // or {"root": "...", "max_incidents": 1}
  }]
}
```

- Devices are resolved by type and position at run time, never by hard-coded
  name, in the same way as `live_campaign_exhaustion.py`. The names in this
  doc's tables are the 2026-10-21 resolutions, for readability.
- `window_s.calibrated` flips to `true` when S3-2 writes back the measured
  median and range.
- `expected_misses` lists detectors that are known not to fire, such as CH-5
  in `all-towers`, so a miss there is not scored as a recall failure.
- `pending` scenarios are runnable but not gated.
- The ledger's `campaign` field carries `"<name>/<run_id>"`, which joins runs to
  scenarios.

The JSON above is annotated with `//` comments for readability. The shipped
file is plain JSON, and a test in S3-1 validates it against a schema.

### 13.2 Abrupt faults (existing physics, mostly existing endpoints)

| Scenario | Injections | Expected detectors | Kind | Detection window (from onset) | Lead over L1 |
|---|---|---|---|---|---|
| `all-chillers` | every DC1 chiller `Chiller_Running=0` (existing) | CR-1 (CHW-starved branch), RM-2, SV-3, CD-1 (as a symptom); L5: one incident rooted at the chiller plant | process | RM-2 4–8 min (2–3 SNMP samples); CR-1 15–17 min | Negative. L1 `Alarm_HighCHWSupply` fires about 3–4 min. CR-1 is a *diagnosis*, not a lead. |
| `all-chwp` | every `CHWP*` `Alarm_Fault=1` | CR-1, RM-2, SV-3; L5 root = pump train | process | as above | Negative (L1 = chiller `Alarm_FlowLoss` after 10 s) |
| `all-towers` | every tower `Fan_Status=0` | CH-5 (expected **miss**, see note), RM-2 later | process | — | CH-5 cannot lead: about 34 s from 30 to 42 °C at full loss is about one BACnet poll |
| `tower-cell-partial` | one tower per DC `Fan_Status=0` at above 60 % plant load | CH-5, CT-2 (*seeded disabled; enable for run*), CH-1 (unloading leaves CHW above setpoint at load below 95 %) | process | CT-2 20–25 min; CH-5 2 samples once the slope is set | CH-5 3–10 min before `Alarm_CondPressLimit`/trip, *if* it trips [uncertain: depends on loss fraction] |
| `all-cwp` | every `CWP*` `Alarm_Fault=1` | CH-5 (likely miss), RM-2 | process | — | as `all-towers` |
| `hall-crahs` | every HA CRAH `Unit_Running=0` | RM-2, SV-3, PD-1; CR-* **must not** fire (gated on running); L5 root = the CRAH group | process | RM-2 4–8 min; SV-3 10 min | n/a (equipment_state is L1) |
| `crah-single-trip` | one CRAH `Unit_Running=0`, sustained 45 min | RM-3, RM-4, SV-1/SV-2 (local), PD-1; L5 **zone** attribution to that CRAH | process | RM-3 15–20 min; PD-1 30–35 min | n/a. The Phase 4 gate: zone beats room-level grouping |
| `crah-coil-starve` | one CRAH `Alarm_HighTemp=1` (needs C5) | CR-1 (CHW-starved), CR-5, RM-3 near the unit | process | CR-1 15–16 min; CR-5 30 min | Negative (the forced bit is the L1) |
| `lead-chiller-stop` | the lead chiller `Chiller_Running=0` | **none** except the L1 state change; CH-4 must not fire on a single start | — | — | Negative control: clean failover |
| `valve-actuator-stuck` | `VCHW-DC1-CP` `Alarm_ActuatorFault=1`, `Position=35` | VA-1 | process | 10–11 min | Negative (the bit is the L1) |
| `valve-feedback-stuck` | `VCHW-DC1-CP` `Position=35` held, **no** bit | VA-1, classified **sensor** (physics unrestricted, DSS:4784) | sensor | 10–11 min | No L1 |
| `chiller-short-cycle` | a non-lead chiller `Chiller_Running` toggled 0/1 every 8 min for 60 min | CH-4 | process | ≤ 60 min (rolling 1 h, ≥ 6 starts) | No L1 |
| `pump-proof-lost` | running `CHWP1-DC1-CP` `Run_Status` sensor fault `stuck: true, stuck_value: 0` (failed current switch: the status reads 0 while the pump turns) | PU-3 (proof = 0 while speed > 10 %) | **sensor** | 60–90 s (2 samples + 60 s) | No L1 |
| `pump-shaft-coupling` | pending S4 `pump_decouple` kind: status 1 and speed hold while flow falls below 5 % | PU-3 (proof = 1 while flow < 5 % after 60 s), then CR-1 if no standby | process | 60–90 s | Seconds before the chiller flow interlock (10 s), at best. **Pending S4.** |
| `cdu-leak` | `CDU1-DC1-HA-R2-01` `Alarm_Leak=1` (existing probe) | SV-1 cluster, SV-3 on that loop; L5 root = CDU | process | SV-3 10 min | Negative (`Alarm_Leak` is L1) |
| `cdu-condensation` | `TCS_Setpoint` ramp 32 → 16 °C at 0.01 K/s on one CDU (needs C9) | CD-6 | process (config) | 10–12 min after TCS − dew point < 2 K | No L1 |
| `utility-flap` | `electrical/utility` DC1 fail/restore 4× in 30 min | AT-1 (*seeded disabled; enable for run*); GN-* context | process | ≤ 30 min | n/a (ATS alarms are L1) |
| `pdu-imbalance` | `pdu_imbalance` pin on one PDU (existing, step to 25 %) | PD-2 | process | 30–40 min (EWMA) | No L1 at this level |
| `valve-hunting` | each of the 4 `valve` devices in turn (`VCHW-DC1-CP` first): `Commanded_Position` and `Position` overrides with `oscillate: {period_s: 240, amplitude: 15}` about the current value, 60 min (needs S1-7) | VA-2 (≥ 6 reversals of ≥ 5 pp in 1800 s); **VA-1 must not fire** (command and feedback move together) | process (physics uncoupled: valve position does not drive header flow, see note) | 30–35 min | No L1 |
| `rpp-trip-crah` | open the mechanical panel feeding half of Hall A's CRAHs: `MPPA-DC1-HA` (feeds `CRAH1-DC1-HA-R9-01/03/05/07` from `MCC1-DC1-MR`). Needs S1-11 (see note). Hold 45 min. | **L5: one incident rooted at `MPPA-DC1-HA`**, through the power-chain hop. RM-2, RM-3, SV-3 and PD-1 attached as symptoms and suppressed as roots. CR-* must not fire on the unpowered CRAHs (gated on running). | process | incident within 10–20 min (RM-2 4–8 min; RM-3 15–20 min) | n/a. The CRAHs' `equipment_state` and the panel's state are L1. The test is root attribution and compression. |

Notes:

- PU-3 has no injection path through overrides. Forcing `Run_Status=0` is read
  as a **command** to stop (`_cmd_off`, BC:735-743), so the pump really stops
  and proof agrees with process. The proof-lost case is therefore a sensor fault
  on a binary point. **Contract deviation (additive):** `stuck_value` (any
  number, used instead of the value at activation) lets `stuck` hold a binary
  at a chosen state. Doc 29 D8 defers standby-fails-to-start. Real BMS points
  pair a start/stop BO with a status BI, so an optional `Start_Command` BV on
  pumps and chillers (S4, driven by `_accrue_run_proof`, DSS:4675) would make
  the original doc 28 form measurable later.
- `valve-hunting`: oscillating the published position of a stand-alone valve
  is honest ground truth for VA-2's signal shape (reversal count). It is not a
  physics fault, because no flow follows the oscillation. Real hunting also
  shows the controlled temperature oscillating in phase. That version is the
  S4 `hunting` kind, which is also the only honest CR-6 truth.
- `rpp-trip-crah` (the name is kept for doc 33's reference): **real CRAHs are
  not fed from IT RPPs.** In this topology, and in most enterprise designs,
  they sit on mechanical distribution (MCC → MPP). An RPP trip would drop IT
  load, which is the opposite symptom. The realistic power-chain root is the
  MPP. There is **no injection path today**: `POST /api/topology/links/break`
  refuses any layer but `production` (`S:api/routers/topology.py:285-287`), and
  `FAULT_MAP` has no `mpp`/`mcc` entry. The comment at `devices.py:1421-1424`
  says a power edge can be broken, but that holds only for the canvas, not the
  REST API [uncertain: whether the canvas path reaches the store's
  energization]. S1-11 adds `mpp_breaker_trip` to `FAULT_MAP`, modelled on
  `swgr_breaker_trip`. It de-energises the panel's loads through the store's
  existing energization, which is the same path a utility loss takes, and is
  ledgered as `electrical`. The CRAH ride-through behaviour follows the
  unpowered-plant path (DSS:4811-4826) [uncertain: whether the 60 s
  ride-through applies to CRAHs or only to chillers; verify in S1-11].
- **CD-1 has no stand-alone abrupt scenario.** The realistic abrupt cause of
  CDU primary-side starvation is a loss of facility water temperature or flow.
  That is a chiller loss or a closed primary isolation valve. The first case
  does saturate the CDU valve in this simulator: the valve demand scales with
  `_avail_design / _avail` (DSS:6208-6216), and CHW supply is driven toward
  25 °C by the 18 K penalty. So CD-1 fires in `all-chillers` and `all-chwp`, but
  as an **L5 symptom of the chiller or pump root**, not as a root, and it is
  scored that way. The second case, a per-CDU primary isolation valve, is not
  modelled. **CD-1 as a root is scored only through `cdu-hx-fouling`** (doc 33
  §P3; fixture-only in P1).
- The `all-towers`/`all-cwp` CH-5 miss is an expected result, not a defect. It
  documents that 30 s polling cannot beat a seconds-scale trip (doc 28 says the
  same about surge).

### 13.3 Gradual faults (need C7–C9)

| Scenario | Injections | Expected detectors | Kind | Detection window | Lead over L1 |
|---|---|---|---|---|---|
| `chiller-cop-decay` | `CHL1-DC1-CP` `cop` → 0.80 at 0.01/h (fast, 20 h) / 0.0006/h (realistic, 14 d) | CH-2. **CH-3 must not fire** (approach unchanged), which is the diagnosis test, scored only once CH-3 is enabled (*pending point availability*). PL-2 not seeded. | process | fast: 3–10 h; realistic: 2–7 d | No L1 |
| `chiller-condenser-fouling` | `hx_approach` +4 K at 0.25 K/h (16 h) | CH-3 (> 3 K above baseline, about 12–14 h; *pending point availability*, needs S1-2 + mapping), CH-2 (COP −10 %); CH-5 only on a hot afternoon | process | CH-2 4–12 h; CH-3 12–16 h | Hours before `Alarm_CondPressLimit`, if reached |
| `tower-fill-fouling` | `CT1-DC1-RF` `tower_approach` +3 K at 0.1 K/h (30 h) | CT-1, then CT-2 at high load (*seeded disabled; enable for run*); CH-2 downstream; L5 root = **tower**, not chiller | process | CT-1 4–15 h | No L1 |
| `crah-filter-loading` | `CRAH1-DC1-HA-R9-01` `filter_loading` → 1.0 at 0.02/h (50 h); fleet churn paused | CR-4 (6–20 h; *pending point availability*, needs S1-5 CRAH `Filter_DP` + mapping), CR-3 (10–30 h, regressed on room load) | process | as listed | 20–40 h before `Filter_Dirty` (L1 at L = 1.0) |
| `cdu-strainer-loading` | `CDU1-DC1-HA-R2-01` `filter_loading` → 1.0 at 0.04/h (25 h) | CD-3, CD-4 | process | CD-3 3–12 h; CD-4 8–20 h | Hours before `Alarm_LowFlow` |
| `cdu-hx-fouling` | `hx_approach` +5 K at 0.2 K/h (25 h) | CD-2 (hours), then CD-1 when the facility valve saturates | process | CD-2 4–12 h; CD-1 5 min after saturation | CD-2: hours. CD-1: minutes before the TCS supply threshold. |
| `chwp-impeller-wear` | `CHWP1-DC1-CP` `pump_derate` → 0.85 at 0.005/h (30 h) | PU-1, PU-2 | process | 6–20 h | No L1 |
| `pump-bearing-heat` | `Motor_Temp` ramp +12 K over 24 h on `CHWP1` | PU-4 | process (uncoupled: real bearing wear also adds vibration and power) | 4–12 h | No L1 |
| `tower-fan-imbalance` | `Vibration` ramp 1.5 → 6 mm/s over 48 h on `CT1` | CT-4 | process (uncoupled) | 6–24 h | Before any platform vibration threshold |
| `tower-basin-leak` | `Basin_Level` ramp 75 → 40 % over 3 h (makeup still flows, store-published) | CT-3 (level falling against make-up flow; *seeded disabled; enable for run*) | process | 30–45 min | Before `Alarm_LowBasin` (operator-only today) |
| `cdu-slow-leak` | interim: `TCS_Loop_Pressure` ramp 250 → 180 kPa over 24 h | CD-5 (**requires S4 `loop_leak`**; detector seeded or PATCHed disabled until then, doc 33 D15). The interim ramp is scored as plumbing only. | process | 4–12 h | Hours before `Alarm_Leak` |
| `crah-setpoint-raise` | HA CRAHs `Setpoint` ramp 22 → 24 °C at 0.5 K/10 min (needs C9) | **none**. CR-1 must follow the setpoint, and RM-4/CR-3 must re-baseline without findings. | context | — | Negative control |
| `chw-reset` | DC1 `CHW_Setpoint` 7 → 9 °C (needs C9 + lift term) | **none**. CH-1 and CD-1 must not fire, and a two-sided CH-2 must treat the COP improvement as excluded (ledger → exclusion). | context | — | Negative control |

### 13.4 Sensor faults (C4) — the sensor-versus-process `kind` test

| Scenario | Injection | Expected detectors | Kind | Detection window |
|---|---|---|---|---|
| `crah-supply-bias` | `Supply_Air_Temp` bias +3 K, monitor mode | DQ-6 (bias) (balance/peer); CR-5; **CR-1 must be masked by L0** (it would otherwise call a control fault) | sensor | CR-5 30–35 min |
| `crah-supply-bias-control` | same, `affects_control: true` | DQ-6 (bias) (physical bound: rack inlets below CRAH supply minus tolerance), RM-4 (zone colder); CR-5 | sensor (process symptoms) | 30–60 min. The hard case: diagnosis accuracy is reported, not gated. |
| `rack-probe-drift` | `SEN1-DC1-HA-R2-01` `inlet_temp` drift 0.1 K/h for 48 h | DQ-6 (bias), RM-4 | sensor | 15–30 h (1.5–3 K offset at SNMP cadence) |
| `pdu-probe-stuck` | a PDU probe `stuck: true` for 6 h | DQ-5 (flatline) (the neighbours move U(±0.3 K)) | sensor | 30 min – 2 h (depends on effective SNMP cadence) |
| `chw-supply-noisy` | `CHL1` `CHW_Supply_Temp` `noise_mult: 5` | DQ-4 (spike) | sensor | ≤ 10 min |
| `crah-return-dropout` | `Return_Air_Temp` `dropout_pct: 30` for 2 h | DQ-1 (dropout) | sensor | ≤ 3 polls per window. **Requires the collector `Status_Flags` task.** |
| `chw-flow-meter-bias` | `CHL1` `CHW_Flow` bias −15 % (as an absolute L/s) | PL-2 (balance; *pending a loop entity*, not seeded), DQ-6 (bias) | sensor | 30–60 min |
| `bmc-inlet-bias` | `SRV01-DC1-HA-R2-01` `inlet_temp` +5 K (Redfish + SNMP) | DQ-6 (bias) (BMC versus PDU probe); SV-1 (negative residual) only once enabled (**SV-1 requires S4 `server_thermal`** and stays disabled until then) | sensor | 30–60 min |
| `tower-wetbulb-bias` | `CT1` `OA_Wet_Bulb_Temp` −3 K | CT-1 classified **sensor**: the sister towers share the sky | sensor | 1–4 h |
| `cdu-filter-dp-drift` | `Filter_DP` drift +2 kPa/h for 12 h | CD-3 classified sensor (pump speed and flow unchanged) | sensor | 3–8 h |
| `ups-battery-probe-drift` | `UPSA-DC1-UR` `battery_temp_c` drift 0.2 K/h for 24 h | DQ-6 (bias); UP-1 classified sensor only once enabled (**UP-1 requires S4 `room_hvac_loss`** for its process truth and stays disabled until then) | sensor | 6–15 h |

### 13.5 Context and negative controls

| Scenario | Injection | Expected | Purpose |
|---|---|---|---|
| `clean-soak-7d` | none | **zero** derived alerts; false findings ≤ 0.05 per device-day (doc 28 gate) | Advisory budget |
| `heatwave` | DC1 weather +8 K dry, +6 K wet, 12 h | **no** CH-2/CT-1 process findings; CT-2 only if the approach itself rises (it should not) | Gating on wet bulb |
| `fire-pre-discharge` | `modbus/fire/smoke` HA (**`--allow-fire` and explicit user approval only**) | CR-*/RM-* explained by fire context. No cooling root. | Must-suppress |
| `maintenance-crah` | platform maintenance window + one CRAH `Unit_Running=0` | suppressed | Context handling |
| `sim-restart` | stop and start the simulator and protocols | DQ-1 (dropout) during the gap only; **no** bias findings after restart (proves C1) | Restart is not a fault |

### 13.6 Detectors without honest ground truth until S4

**CD-5, SV-1, UP-1 and TX-1 require S4.** Doc 29 seeds them enabled. Doc 33
(D15, Q17) recommends `enabled = false`, or a PATCH to off before the first
Phase 3 fit, until S4 gives them physics. Their scenarios in this library carry
`"pending": "s4"` and are not gated. GN-1, CR-2, PL-1, CR-6, SV-2 and UP-2 are
also S4-dependent, but doc 29 seeds them on or off independently (doc 33 §7).

> **Contract deviation (additive degradation kinds).** The contract's five
> kinds cannot express these, and ramping an uncoupled point would test the
> plumbing, not the detector.

| Detector | What is missing | Proposed kind | Real mechanism | Interim |
|---|---|---|---|---|
| GN-1 | fuel falling while the genset is stopped | `fuel_leak` (L/h on `gen_fuel_pct` while stopped) | Tank or line leak | none |
| CD-5 | real loop-mass loss | `loop_leak` (kPa/h, coupled to `Alarm_Leak` at a threshold) | Fitting weep | `cdu-slow-leak` ramp |
| UP-2 | battery capacity fade | `battery_aging` (runtime factor) | VRLA ageing | `ups_batt_health` pin [uncertain whether runtime follows it] |
| UP-1 (process) | battery-room cooling loss | `room_hvac_loss` on the UPS room | Split-unit failure | sensor variant only |
| TX-1 | transformer cooling loss | `xfmr_cooling` (oil rise multiplier, τ 3 h, DSS:382-410) | Fan or radiator fouling | weather override, as a *negative* control |
| SV-1 | rising die-to-inlet thermal resistance | `server_thermal` (K offset on the die target) | Heatsink dust, paste | none (`temp_high` is abrupt and L1) |
| SV-2 | fan wear | `server_fan_wear` (rpm per duty) | Bearing wear | none |
| CR-2 | bypass air | `bypass` (BYPASS_FRAC per unit, `air_model.py:45`), ideally driven by the existing containment doors (`/modbus/doors`) | Open containment door, missing blanking | return-sensor bias (sensor kind only) |
| PL-1 (seeded disabled, doc 29 D11) | low ΔT syndrome | `hx_approach` on **CRAH** (coil fouling). Needs CHW header flow to follow valve demand. Today flow follows load (CM:564). | Coil fouling, bypass | none |
| CR-6 / VA-2 (CRAH) | CRAH valve hunting | `hunting {period_s, amplitude_pct}`, applied in the CRAH block with supply oscillating in phase | Mistuned PID | VA-2 only: `valve-hunting` (§13.2) on the 4 valve devices |

## 14. Acceptance

- [ ] `DCIM_SIM_SEED=42`: two process starts with different `PYTHONHASHSEED`
      give identical plant-engine initial offsets and identical per-instrument
      offsets (test runs a subprocess).
- [ ] The shipped topology has zero devices whose `datacenter_city` disagrees
      with their DC majority. `city_by_dc` and `dc_city` come from one helper.
- [ ] After a data fix, SNMP Stop → Regenerate → Start; `sysLocation` of
      `SEN1-DC2-HA-R2-01` reads New York.
- [ ] `POST /api/faults/sensor` with bias +3 K on a CRAH `Supply_Air_Temp`:
      BACnet wire = truth + 3.0 ± 0.05 after 2 ticks; `_plant_state_cache`
      value, room supply and every rack inlet in the hall are **unchanged**
      against a control run (offline fixture, same seed).
- [ ] The same fault is visible on the CRAH SNMP card OID after the next SNMP
      shard pass.
- [ ] A Redfish `Thermal` inlet bias is visible in Redfish and SNMP for the
      same server, and `device.inlet_temp` is unchanged.
- [ ] A Modbus/SNMP ext bias on `battery_temp_c` is visible on both and
      `_ext_state_cache` is unchanged.
- [ ] `affects_control: true` on CRAH supply: the reading holds setpoint ±0.2 K
      and the zone's rack inlets fall by the bias ± 0.3 K after settling.
- [ ] Stuck: zero variance on the wire for the fault's duration and no BACnet
      COV for that point. Dropout: `Reliability=NO_SENSOR` and the FAULT flag
      are set during windows on BACnet; `Reading: null` on Redfish.
- [ ] A forced CRAH `Alarm_HighTemp` raises the published supply by ≥ 5.5 K
      within 60 s and warms the room. Clearing restores it.
- [ ] `cop` → 0.80: published COP falls 20 % ± 2 % at constant load, and
      `COP × Active_Power` still equals the delivered kW ± 2 %.
- [ ] `filter_loading` → 1.0 on a CRAH: `Filter_DP` rises monotonically, fan
      speed rises and delivered stays 1.0 until saturation, and `Filter_Dirty`
      = 1 exactly when L ≥ 1.0.
- [ ] Each degradation persists across a simulator restart with its value
      intact, and a `resume` event is ledgered.
- [ ] A ramp on a store-published point returns 409, naming the right tool. A
      ramp on `Motor_Temp` reaches its target at the stated rate ±1 %.
- [ ] CRAH `Setpoint` 22 → 24 moves the published supply and the room. A CHW
      setpoint override is **not** overwritten (it reads 9.0 on the wire).
- [ ] Weather +8/+6: the tower `OA_*` points move by +8/+6. Wet bulb never
      exceeds dry bulb. The override expires at `until` and is ledgered.
- [ ] `GET /api/air/influence` in spatial mode: weights per rack sum to 1 ± 1e-6
      and equal `air_model.influence` normalised. Uniform mode returns 409.
- [ ] The ledger holds an entry for an injection made through **each**
      channel in §7.1. It survives a restart, and a truncated last line is
      skipped.
- [ ] `tools/live_campaign_anomaly.py` without `--send` performs no POST.
      `fire-pre-discharge` refuses without `--allow-fire`.
- [ ] Each §13 scenario has had one live run. Its measured detection window is
      written back into `anomaly_campaigns.json`, replacing the design target.
- [ ] The full simulator test suite passes. `graphify update .` has been run.

## 15. Ordered task list

Each task is one reviewable unit. S0 is one simulator restart, shipped in
platform Phase 0. **S1, S2 and S3 ship in the platform Phase 1 slot** (doc 33
D20, tasks 1.22-1.28). S1 is one BACnet Stop → Start, and its mapping rows ride
on Phase 1's single collector rebuild (doc 33 1.22). S1-8 must land before any
setpoint negative control is scored (doc 33 R10). S4 is optional and goes
alongside platform Phase 3 (doc 33 3.14).

| # | Task | Files | Tests |
|---|---|---|---|
| **S0-1** | Stable hashing + global seed | new `S:core/sim_seed.py`; `S:app/main.py`; BC:311, 452, 535; BPG `__init__` + :465, 517, 618 (`self._rng`); DSS:6316, 7446, 7450, 8537; `S:core/snmprec_generator.py:2480`; `S:core/trap_engine.py:1162`; `auth.env.example` | new `tests/test_sim_seed.py` |
| **S0-2** | DC city: helper + data fix | DSS:1564-1571, 3661-3674; new `S:tools/fix_dc_city_labels.py`; `S:topologies/dual_dc_enterprise.json` (10 devices) | new `tests/test_dc_city.py` |
| **S0-3** | Ledger core + `GET /faults/ledger` + router registration | new `S:core/fault_ledger.py`; new `S:api/routers/faults.py`; `S:api/main.py` (include router) | new `tests/test_fault_ledger.py` |
| **S0-4** | Ledger hooks on existing channels | `S:api/routers/bacnet.py` (`set_plant_override`, utility, ats); `S:api/routers/devices.py` (`set_device_fault`, `set_device_override`); `S:api/routers/modbus.py` (fire); `S:api/routers/topology.py` (links); `S:api/routers/redfish.py` (action); `S:api/routers/tick.py`; `S:api/routers/fleet.py`; protocol start/stop in `S:api/state.py` | extend `tests/test_fault_ledger.py` with one case per channel |
| **S0-5** | `FaultBook` + `SensorFault` + `DeviceView` | new `S:core/fault_injection.py`; DSS (`self.faults`, `_plant_wire_cache`, `_get_plant_wire_state`, `_publish_plant_state`) | new `tests/test_fault_injection.py` (maths, seeding, expiry, stacking 409) |
| **S0-6** | Wire the transform into every renderer | BC `tick` (between :821 and :836, `get_truth_snapshot`); `snmprec_generator.py` (:830 view, :1331 wire, the ext helper); `S:simulator/modbus_controller.py:494`; `S:simulator/redfish_device.py:450-453` + the event path; DSS `_publish_facts` (view); `bacnet.py:524` `?view=` | new `tests/test_fault_publish_truth.py` (truth unchanged, wire biased, per protocol) |
| **S0-7** | `POST/DELETE/GET /faults/sensor`, `/faults`, `/faults/{id}`, `/faults/points` | `S:api/routers/faults.py`; new `S:core/fault_points.py` (namespace + metric hints) | API tests in `tests/test_faults_api.py` |
| **S0-8** | `affects_control` | DSS CRAH block (:6096), `_compute_chw_loop` (:5767), CDU block (:6216) | in `test_fault_publish_truth.py` |
| **S0-9** | CRAH `Alarm_HighTemp` store-side coupling + forced-coupling log | DSS CRAH block (:6096-6147) | new `tests/test_crah_hightemp_coupling.py` |
| **S0-10** | Influence export | `S:core/air_model.py` (`influence_matrix`); DSS `_solve_room_air` (:6646-6698); `faults.py` `/air/influence` | extend `tests/test_air_model.py`, `tests/test_air_model_store.py` |
| **S1-1** | `Degradation` + persistence (`active.json`) + resume | `S:core/fault_injection.py`; `S:core/fault_ledger.py`; DSS start/stop | new `tests/test_degradation.py` |
| **S1-2** | `cop` + `hx_approach`(chiller) + new chiller points | DSS:4309 and the COP block :4412-4466; BPG PLANT_SPEC `chiller` (append, plus walk `continue` guards) | `test_degradation.py`: identity `Q = COP × kW`, 200-tick settle |
| **S1-3** | `tower_approach` | DSS:5030 | `tests/test_cooling_tower.py` extension |
| **S1-4** | `hx_approach`(CDU) + live `Approach_Temp`/`Filter_DP` | DSS CDU block :6195-6235; BPG walk guards | `test_degradation.py` |
| **S1-5** | `filter_loading` (CRAH + CDU) + CRAH `Filter_DP` point | DSS :6096-6147, :6465-6487; BPG PLANT_SPEC `crah` (append) | `test_degradation.py`, `tests/test_crah_group_control.py` extension |
| **S1-6** | `pump_derate` | DSS :6029 | `test_degradation.py` |
| **S1-7** | Ramp + oscillate on overrides | `S:api/routers/bacnet.py` (`PlantOverride`, `set_plant_override`); DSS ramp evaluation before :6830 | new `tests/test_plant_override_ramp.py` |
| **S1-8** | Setpoints honoured by physics + CHW lift term | DSS :52/6096/6564/6660, :5767/5904/5915-5920/6174/6208-6222; CM chiller draw | new `tests/test_setpoint_events.py`; re-run `tests/test_cooling_calibration.py` (design anchor unchanged at SP 7.0) |
| **S1-9** | Weather override | `S:core/cooling_model.py` (:106-147); `faults.py` `/weather`, `/weather/override` | new `tests/test_weather_override.py` |
| **S1-10** | CDU `Alarm_HighSupplyTemp` store-side coupling | DSS CDU block | `test_crah_hightemp_coupling.py` (rename to `test_forced_couplings.py`) |
| **S1-11** | `mpp_breaker_trip` (and `mcc_breaker_trip`) in `FAULT_MAP`, de-energising downstream plant through the store's existing energization; ledgered as `electrical`. Enables `rpp-trip-crah`. | `S:api/routers/devices.py` (`FAULT_MAP`), DSS energization (the MPP/MCC branches near :3217, :3272) | new `tests/test_mpp_trip.py`: the 4 CRAHs fed by `MPPA-DC1-HA` are unpowered, the other 3 are not, and clearing restores them |
| **S2-1** | Web UI: Faults tab, dialogs, override ramp, client | `S:webui/src/components/RightPanel/FaultsPanel.tsx` (new), `RightPanel/index.tsx`, `TopologyCanvas/NodeContextMenu.tsx`, new `SensorFaultDialog.tsx`, `DegradeDialog.tsx`, `S:webui/src/api/client.ts` | `tsc` clean. Verify in the running app at `http://localhost:8001/web/` after the user restarts. |
| **S3-1** | Campaign library + runner | new `S:tools/anomaly_campaigns.json`, new `S:tools/live_campaign_anomaly.py`; `S:tools/_live_api.py` (fault helpers) | offline twins of 4 scenarios in `tests/test_fault_campaign.py` (`crah-supply-bias`, `chiller-cop-decay` compressed with pinned `_dt`, `crah-filter-loading`, `lead-chiller-stop`) |
| **S3-2** | Calibration runs; write the measured windows back | `S:tools/anomaly_campaigns.json` | acceptance §14 |
| **S4-x** | Additive kinds from §13.6, one per task, highest value first: `hunting`, `loop_leak`, `server_thermal`, `bypass`, `fuel_leak`, `battery_aging`, `xfmr_cooling`, `server_fan_wear`, CRAH coil + valve-driven CHW flow; plus `Start_Command` points | `S:core/fault_injection.py`, DSS, BPG | one test file each |

After every task: run the full simulator test suite and `graphify update .`,
and ask the user to restart the simulator, carrying `DCIM_SIM_AIR_MODEL=spatial`
and restarting the protocols. The user commits.

## 16. Contract deviations, collected

| # | Deviation | Recommendation |
|---|---|---|
| D1 | Sensor body gains `affects_control`, `dropout_window_s`, `campaign` | Additive and optional. The defaults equal the contract semantics. |
| D2 | `GET /api/faults/points`, `GET /api/faults/{id}` | Additive |
| D3 | Weather override needs a site: optional `datacenter` (default all); `DELETE` and `GET /api/weather` | Additive. The contract body stays valid. |
| D4 | Plant overrides gain `oscillate` besides `ramp`; ramps refused (409) on store-published points | Additive. The refusal prevents silent no-op injections. |
| D5 | Ledger covers every injection channel and adds `device_id`, `expected_kind`, `metric_hint`, `source`, `campaign`, `end_reason`, `marks`, `resumes`, `sim` | Additive. Required for honest event scoring. |
| D6 | New BACnet points `Cond_Approach_Temp`, `Evap_Approach_Temp` (chiller), `Filter_DP` (CRAH); optional S4 `Start_Command` (pump, chiller) | Resolves doc 29 D9 (CR-4) and D10 (CH-3). Platform half of the metric-key chain: mapping rows, embedded copy, collector rebuild, re-discovery. No new registry keys. Then enable CR-4 and CH-3. |
| D7 | Additional degradation kinds (§13.6) | Needed to cover GN-1, CD-5, UP-1 (process), UP-2, TX-1, SV-1, SV-2, CR-2, PL-1 and CR-6 honestly |
| D8 | L0 checks have no IDs in doc 28 | Adopted doc 29 D1: DQ-1..DQ-6. DQ-6 needs baselines (0113). |
| D9 | `PYTHONHASHSEED` (doc 28) replaced by stable hashing | Simpler and launch-script independent |
| D10 | `GET /api/air/influence` returns 409 in uniform mode | An honest refusal. There is no structure to export. |
| D11 | `GET /api/bacnet/plant/metrics?view=truth|wire` | Additive; the campaign harness needs it to verify a sensor fault landed |
| D12 | Sensor body gains `stuck_value` | Additive. It is the only way to inject PU-3's proof-lost case (doc 29 D8), because a `Run_Status=0` override is read as a stop command. |
| D13 | Campaign expectations follow doc 29's seeded state | Pending point availability: CR-4, CH-3. Pending a loop entity: PL-2. Enable for run: PL-1, CT-2, CT-3, AT-1. VA-1 is valve-only. |
| D14 | `rpp-trip-crah` is rooted at the MPP, not an RPP | CRAHs are fed from mechanical panels. A new `mpp_breaker_trip` (S1-11) is needed, because the REST link-break refuses the power layer. |
| D15 | S1 moved into the platform Phase 1 slot (doc 33 D20) | Task ordering in §3 and §15 follows doc 33 |
