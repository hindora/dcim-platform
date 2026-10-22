# 33 — Anomaly detection: implementation plan

Status: **plan. Nothing started.** This plan follows `28` (design), `29` (data
model), `30` (API), `31` (frontend) and `32` (simulator changes). It owns the
phase order, the task list with files and estimates, detector parameters, fault
campaigns, the scoring harness, deploy and rollback, and the risk register. It
does not repeat DDL (`29`), endpoint shapes (`30`), screens (`31`) or simulator
internals (`32`). Where a value overlaps, the owning document wins and this one
points to it. Simulator work is referenced by `32`'s task IDs (S0-1 … S4-x), and
campaigns use `32` §13's scenario names exactly.

Every phase can ship on its own, and every exit criterion is checked by running
something. **Every numeric target in this document is a starting value.** None
is a sourced industry benchmark, because `28` found no such benchmarks exist.
The harness in §4 exists to move these numbers.

`P:` means `dcim-platform/` and `S:` means `Datacenter_Network_Simulator/`. Lines
were checked against the tree on 2026-10-21.

---

## Summary (one page)

| Phase | Ships | What the operator sees | Effort | Calendar (incl. soak) |
|---|---|---|---|---|
| 0 Ground truth + plumbing | Simulator S0 (`32` §15): stable seed, DC city fix, ledger, **truth/wire split** with the publish-layer sensor transform, the CRAH `Alarm_HighTemp` coupling fix, influence export. Platform: `anomaly_*` tables (0111), `analytics/stats.py`, an empty sweep with a lock and cadence gate, `/anomalies/health` | Nothing on the console, by design. `/health` reports sweep timings | ~122 h | 3 wk |
| 1 L0 + L2 rules | 13 enabled G36-style rules raised as `detection=derived` alerts **capped at MINOR**. Sensor-health findings. **Collector reads BACnet `Status_Flags`**. Simulator S1 (degradations, ramp/oscillate, setpoints honoured, weather, new chiller/CRAH points) and the campaign runner, all in the same slot. One collector rebuild. Alarm evidence panel, detection filter, detector settings | Derived alerts in the alarm list and the "Cooling & Environment" strip, each with evidence | ~237 h | 5 wk + 7-day soak |
| 2 L5 cooling correlator | Cooling dependency graph, incidents, symptom suppression, fire/maintenance/standby context, incident tree in the evidence panel. MAJOR is allowed only for an L5 incident root, and only after the gate | One incident per cascade instead of an alert storm | ~70 h | 2 wk (built during the P1 soak) |
| 3 L3 residuals | Nightly baselines, 20 residual/CUSUM detectors plus DQ-6, CR-4/CH-3 enabled on the new points, triage tab `/analytics?view=anomaly`, expected band and "Under watch" panels | Advisory queue sorted by `impact_kw`, and expected-versus-observed on each device | ~145 h | 4 wk + gradual campaigns + 7-day soak |
| 4 L4 peer + zones | Sibling robust z, aisle residuals, a fitted rack↔CRAH influence matrix, sensor/process `kind` | Zone-level incident roots; sensor findings split from process findings | ~82 h | 3 wk |
| 5 L6 explainer (optional) | Evidence bundle, pluggable explainer (none/cloud/local), validator, audit, thumbs | AI-labelled narrative shown below the evidence | ~54 h | 2 wk |

**Total: ~710 h (range 570–850 h, −20 %/+20 %).** The optional simulator S4
kinds add about 30 h; they give GN-1, CD-5, UP-1/2, TX-1, SV-1/2, CR-2, PL-1 and
CR-6 honest ground truth. **Calendar: 19–25 weeks**, set by soaks and history
accrual rather than build hours.

**Critical path**

```
S0 (truth/wire split, ledger) ─► P0 platform sweep ─► P1 rules + collector Status_Flags + S1 ─► P1 7-day soak ─► P2 correlator
     │                                                   │
     │                                                   └─► new CRAH Filter_DP / chiller approach points start accruing history
     └─► P0 deploy starts the CLEAN-HISTORY CLOCK (spatial mode, DCIM_SIM_SEED, DC2 city fixed)
               ├─ ≥14 days ─► P3 baselines can fit ─► P3 gradual campaigns (wall-clock days) ─► P3 soak
               └─ ≥28 days ─► P4 influence fit
```

Two schedule decisions matter most:

- **Ship S0 as early as possible, then leave the simulator alone.** Phase 3
  needs 14 days of clean post-fix history, and Phase 4 needs 28 days in
  `spatial` mode.
- **Ship S1 with Phase 1, not Phase 3.** Phase 1 already rebuilds the collector
  for `Status_Flags`, so the new BACnet points' mapping rides along on that one
  rebuild. Their history is about 5 weeks old when Phase 3 fits. S1-8
  (setpoints honoured) is also what makes Phase 1's setpoint negative controls
  meaningful.

L6 (Phase 5) is off the critical path. Every Phase 1–4 criterion must pass with
`DCIM_ANOMALY_EXPLAINER=none`.

---

## 1. Standing constraints

1. **The LLM is never in detection or actuation.** No path from
   `analytics/explainer.py` reaches a write client, a setpoint, an acknowledge or
   `raise_alarm` (a Phase 5 test enforces this).
2. **`source="analytics"` raises are capped at MINOR until the Phase 2 gate.**
   Under the default policy, a MAJOR `alarm`-class alarm opens a Jira ticket
   after 300 s (`P:backend/app/integrations/policy.py:45-99`). A MAJOR derived
   alarm raised before L5 exists would ticket every symptom.
   - The cap lives in `analytics/sweep.py`, as `30` recommends.
   - `29`'s `ck_anomaly_detector_severity` keeps the seeded defaults at
     MINOR/WARNING.
   - After Phase 2, MAJOR is reachable only by L5 escalating an incident root's
     alarm. No derived type is ever allow-listed (`policy.py:57`).
   - The alternative is an `exclude_detections` clause (Q13).
3. **Advisories never page.** Promotion requires corroboration (`29` §9.2).
4. **Every detector with an `alarm_type` ships its taxonomy entries in the same
   phase.** That means `BY_ALARM_TYPE` **and** `NON_TRAP_ALARM_TYPES` (§2): task
   1.12 for the L2 types, task 3.5 for the L3 promotion types.
5. **No analytics raise passes a state key as `metric_key`**, because
   `detection_for` returns `state` for those (`alert_taxonomy.py:704-705`;
   `29` D14).
6. **Raw-SQL tables, no hypertables** (`29` §3). Retention is a nightly batched
   prune.
7. **No new Python dependencies.** `analytics/stats.py` is pure Python.
8. **Detector scores are not metric-registry keys.**
9. **One platform deploy per phase** (§5). Run `XINFO GROUPS telemetry.v1` before
   and after. When a phase needs simulator work, the simulator goes in the same
   slot, first.
10. **Nothing in `ingest/worker.py` goes below `__main__`** (`worker.py:1349`;
    `tests/test_room_topic.py:36`). After every deploy, check `dev.log` for
    `ingest tick failed`.
11. **CI gates before every push:** `ruff check app tests`, `pytest -q`,
    `alembic upgrade head`/`downgrade base`, `npm run build`,
    `validate_palette.js`; plus for collector changes `go mod tidy` drift,
    `gofmt`, `go vet` and `go test` (`P:.github/workflows/ci.yml`). Check CI over
    the REST API after every push.
12. **The user commits backend, docs, collector and simulator changes.** The
    frontend ships per phase under the standing rule (commit, push, check CI,
    pull in WSL, restart) after loading the ui-ux-pro-max and frontend-design
    skills.
13. **Web UI only.** Colours come only from `:root` tokens. Use the Assets chart
    forms and `components/Pagination.tsx`. Port `NumberInput` first (1.17). The
    home page is out of scope (`31`). On the simulator side, its own web UI is
    edited but never built by hand; `run.sh` builds it.
14. **Campaign tools default to dry run.** `live_campaign_anomaly.py` needs
    `--send`, and fire additionally needs `--allow-fire` **plus the user's
    explicit approval for that run**. A fire scenario never runs on live without
    being asked.

---

## 2. Verified code seams

| Seam | Where (verified) | Consequence |
|---|---|---|
| Worker loop | `P:backend/app/ingest/worker.py:238-276`; timers `-inf` at `:164-174` | `_maybe_detect_anomalies()`/`_maybe_fit_baselines()` go after `_maybe_snapshot()`, inside the class, above `_amain` (`:1334`) |
| Sweep template | `_maybe_sweep_staleness` `:461-491` | Copied exactly: own `unit_of_work()`, outbox in the same transaction, fanout after commit, catch-all |
| Locks | `assigner.py:53,135` ("DCIM5"), `rollout.py:48,243` ("DCIM7"), `collector_visibility.py:45,94` ("colv") | Per `29` §10.1. Sweep: `pg_try_advisory_xact_lock(0x44_43_49_4D_41)`. Fit: session `pg_try_advisory_lock(0x44_43_49_4D_42)` on a dedicated connection, with per-batch transactions and a once-per-UTC-day guard in Postgres |
| Unaligned replicas | `docker-compose.yml:113`; `dev.sh:189`; `worker.py:164-174` | **Cadence gate**: skip when `dcim:anomaly:sweep.finished_at` is newer than 0.9 × `SWEEP_S` (`29` §10.1) |
| Settings | `config.py:18-24`; `get_settings` `@lru_cache` `:201-202` | An env flip needs a restart; detector `enabled` does not |
| Stats to lift | `services/forecast.py`: `t95 :87`, `median :97`, `mean :108`, `detect_level_shift :112`, `fit_linear :194` | Lifted into `analytics/stats.py` and re-imported by forecast. `tests/test_forecast.py` stays unchanged as proof |
| Thermal verdicts | `services/thermal.py`: `:44,48,52,177,184,187`; `classify_crah :132-171`; `classify_cdu :229` | Constants move to `analytics/catalogue.py` and are imported back |
| Plant verdicts | `services/plant.py`: `VALVE_DEVIATION_PCT :54`, verdict `:653-657` | VA-1 on `valve` devices only (`29` D7) |
| Taxonomy | **`P:backend/app/core/alert_taxonomy.py`**: `:177-190`, `BY_ALARM_TYPE :328`, `detection_for :694-707` | New types need `BY_ALARM_TYPE` and `NON_TRAP_ALARM_TYPES` (`tests/test_alert_taxonomy.py:282-323`); the check is two-way (`:340-353`) |
| Alarm write | `raise_alarm` `repositories/alarms.py:217-226`; `alarm.attributes` jsonb exists (`0005_alarms_and_events.py:99`) | Optional `attributes` kwarg, no migration |
| Device rollup | Callers run `refresh_device_alarm_state` (`alarms/service.py:254,305,409`) | The sweep calls it for every device it touched |
| Fanout / hub | `fanout.py:99,122`; `hub.py:46` `Queue(256)`, session closed on `QueueFull` (`:170-178`) | Topic `anomalies`; **≤ 50 frames per sweep** |
| BACnet quality | Collector stamps every BACnet sample GOOD (`P:collector/internal/adapters/bacnet/bacnet.go:347`) | Simulated BACnet dropout (hold value, `Reliability=NO_SENSOR`, FAULT flag; `32` §4.5) is **invisible to L0** until the collector maps `Status_Flags`/`Reliability` to quality (task 1.21) |
| Auth in tests | `POST /login` always issues `admin` (`api/v1/misc.py:50-58`) | 403 tests mint a viewer token with `issue_token(..., "viewer", ...)` |
| Telemetry chart | Tab draws **`TimeChart`** (`DeviceHistory.tsx:5,175,200`). `Plot.tsx:107-109` collapses the y-range when a band bound is missing | Extend `TimeChart`; leave `Plot` alone (`31` D2). `chartGroups.ts:64-71,86-92` lacks `cop`/`approach_temp`/`filter_diff_pressure`, hence "Under watch" panels |
| Alarm list | No on-screen detection filter; `client.ts:2665-2673` already types `detection` | `?detection=` select (`31` §8.3) |
| NumberInput | Absent in `P:frontend`; exists at `S:webui/src/components/NumberInput.tsx` | Port it (`31` D1) |
| Frontend tests | `frontend/package.json` has no test runner | Q15 |
| Backend tests | 148 files, ~2,100 tests; DB tests skip without `DCIM_TEST_DATABASE_URL` | `_live.py` tests run against a throwaway DB; only CI exercises downgrades |
| Metric keys | `approach_temp` and `filter_diff_pressure` exist (`contracts/metrics/registry.yaml:625,673`) | CR-4/CH-3 need **mapping rows only**: `objects.yaml` + embedded copy (`scripts/sync_mapping_bundle.py`), collector rebuild, BACnet re-discovery (`32` D6). No new registry key |
| Migrations | Head is `0110_an_endpoint_can_have_a_write_credential.py` | 0111–0115, named in `29` §13 |

---

## 3. Phases

Effort key: **S** ≤ 4 h, **M** 4–12 h, **L** 12–32 h. Hours are stated per task.
"Dep" lists prerequisite tasks.

### Phase 0 — Ground truth and plumbing

**Goal.** Make faults injectable and scorable with physics left true. Give the
platform its tables and a sweep that does nothing yet, and measure that sweep on
the live host.

**Simulator (`32` §15, S0, one simulator restart)**

| # | Task (`32` ID) | Files (per `32`) | Effort | Dep |
|---|---|---|---|---|
| 0.1 | S0-1: stable hashing + `DCIM_SIM_SEED` (replaces `PYTHONHASHSEED`, `32` D9) | `S:core/sim_seed.py` (new), BC:311,452,535, BPG, DSS call sites, `auth.env.example` | S 4 | — |
| 0.2 | S0-2: DC city helper + data fix for 10 devices | DSS:1564-1571,3661-3674, `S:tools/fix_dc_city_labels.py`, `S:topologies/dual_dc_enterprise.json` | S 3 | — |
| 0.3 | S0-3/S0-4: ledger core + `GET /faults/ledger` + hooks on **every** existing injection channel | `S:core/fault_ledger.py`, `S:api/routers/faults.py`, routers for bacnet/devices/modbus/topology/redfish/tick/fleet, `S:api/state.py` | M 14 | — |
| 0.4 | S0-5/S0-6: `FaultBook` + **truth/wire cache split**, with the transform wired into every renderer (BACnet, SNMP, Modbus, Redfish) | `S:core/fault_injection.py`, DSS, BC tick, `snmprec_generator.py`, `modbus_controller.py:494`, `redfish_device.py:450-453`, `bacnet.py:524` `?view=` | L 34 | 0.3 |
| 0.5 | S0-7/S0-8: `/faults/sensor` API, point namespace, `affects_control` | `S:api/routers/faults.py`, `S:core/fault_points.py`, DSS CRAH/CHW/CDU blocks | M 12 | 0.4 |
| 0.6 | S0-9: CRAH `Alarm_HighTemp` store-side coupling (confirmed overwritten in code, `32` §9.2) + forced-coupling log. **CR-1's ground truth depends on it** | DSS CRAH block :6096-6147 | S 4 | 0.4 |
| 0.7 | S0-10: `GET /api/air/influence` (409 in uniform mode) | `S:core/air_model.py`, DSS `_solve_room_air`, `faults.py` | M 6 | — |

**Platform**

| # | Task | Files | Effort | Dep |
|---|---|---|---|---|
| 0.8 | Migration **0111** `0111_an_anomaly_is_a_finding_not_an_alarm.py` (`29` §13) | P: `backend/alembic/versions/` | M 6 | — |
| 0.9 | Settings per `29` §11 | P: `backend/app/core/config.py` | S 1 | — |
| 0.10 | `analytics/stats.py`: lifted functions + EWMA, two-sided CUSUM, Hampel, robust z/MAD, normal-equation OLS (≤ 6 regressors), power-iteration PCA | P: `analytics/__init__.py`, `analytics/stats.py`, `services/forecast.py` | M 10 | — |
| 0.11 | `analytics/catalogue.py`: registry, override merge, G36 missing-point disable, `DERIVED_SEVERITY_CAP = "MINOR"` | P: `analytics/catalogue.py` | S 3 | 0.8 |
| 0.12 | `analytics/sweep.py` `run_sweep(session, now)`. Worker steps with the DCIMA xact lock **plus the cadence gate**, and DCIMB session lock with a daily guard. Both above `__main__`. Heartbeat `dcim:anomaly:sweep`. Structlog and Prometheus | P: `analytics/sweep.py`, `ingest/worker.py`, `core/metrics.py` | M 6 | 0.9, 0.11 |
| 0.13 | `repositories/anomalies.py` (checkpoint/restore `29` §10.3-10.4, health), `schemas/anomalies.py`, `api/v1/anomalies.py` with **`GET /health` only**, including `enabled`, `explainer`, `sweep_s`, `baseline_last_fit_at` (`31` D6) | P: those files + router include | M 6 | 0.8 |
| 0.14 | `raise_alarm(..., attributes=None)` merged into `alarm.attributes` | P: `repositories/alarms.py:217` | S 3 | — |
| 0.15 | Scoring library + CLI skeleton (§4), reading `S:tools/anomaly_campaigns.json` | P: `backend/app/analytics/evaluation.py`, `backend/scripts/anomaly_eval.py` (new) | M 8 | 0.3 |
| 0.16 | Pre-detector measurement: ingest ticks/min, `XINFO` slope, worker RSS, empty-sweep time | recorded in this doc | S 2 | 0.12 |

> **Contract deviation — evaluation files.** The scorer has no home in the
> contract. It goes in `app/analytics/evaluation.py` and
> `backend/scripts/anomaly_eval.py`.

**Unit tests**

| File | Asserts |
|---|---|
| `backend/tests/test_anomaly_stats.py` | EWMA closed form. CUSUM (k = 0.5σ, h = 5σ) signals a +1σ step in 6–16 samples and stays silent on a seeded 300-sample in-control series. Built note: the two-sided in-control average run length is only ~465–500 samples, so ~45 % of random 300-sample series do signal; the test pins seed 0 and the measured run length. At 60 s sweeps that is one false CUSUM alarm per series roughly every 8 h, so Phase 3 must raise h or gate on corroboration. Hampel flags a spike but not a step. Robust z exact. OLS to 1e-9, singular design refused. PCA recovers a 3×3 eigenvector |
| `backend/tests/test_forecast.py` (unchanged) | The lift changed nothing |
| `backend/tests/test_anomaly_sweep_live.py` (DB) | Exactly one of two concurrent sweeps holds DCIMA. A second holder 30 s later is refused by the cadence gate. With no gate key, it still runs once |
| `backend/tests/test_anomaly_worker_wiring.py` | No-op when disabled. `SWEEP_S` honoured. Methods sit above the entry point |
| `backend/tests/test_alarm_attributes_live.py` (DB) | Attributes stored on create and merged on conflict. Omitted means untouched |
| `backend/tests/test_anomaly_eval.py` | Matching, tolerance across repeated runs, delay, lead, false per device-day, compression (§4) |
| `backend/tests/test_anomaly_health_api.py` | Shape. Readable with a hand-minted viewer token (`/login` only issues admin) |
| S: `tests/test_sim_seed.py`, `test_dc_city.py`, `test_fault_ledger.py`, `test_fault_injection.py`, `test_fault_publish_truth.py`, `test_faults_api.py`, `test_crah_hightemp_coupling.py`, `test_air_model*.py` | Per `32` §15. The load-bearing one is `test_fault_publish_truth.py`: **truth unchanged, wire biased, for every protocol** |

**Campaigns.** No scored scenario. Run `32` §14's bias acceptance check (+3 K
on a CRAH `Supply_Air_Temp`). Wire must equal truth + 3.0 ± 0.05, the room is
unchanged, and the platform's `telemetry_sample` holds the biased value. Also run
`sim-restart` once to confirm restart is not a fault: DQ-1 is not live yet, so
this checks only that bias findings and stepped offsets are absent.

**Deploy (§5).**

1. Simulator S0 restart, carrying `DCIM_SIM_AIR_MODEL=spatial` and `DCIM_SIM_SEED`
   (in `auth.env`).
2. **SNMP Stop → Regenerate → Start**, because the C2 data fix changes the
   topology fingerprint (`32` §8). Re-import the topology on the platform, or let
   discovery refresh the 10 sensors' location.
3. Start every protocol again. Start SNMP on its configured port (`32` §8).
4. Ledger the C1 one-time offset step as `context: sim_change` and post a
   baseline exclusion covering it.
5. Platform restart (0111, flag true, no detectors).

**The clean-history clock starts here; record the timestamp.**

**Exit criteria**

- [ ] 60 ± 2 sweeps per hour across both workers, one at a time; the other
      worker logs `lock_held`/`cadence`.
- [ ] Empty-sweep p95 < 0.5 s.
- [ ] Ingest ticks/min and `XINFO` lag within 10 % of 0.16 after 30 min, with a
      non-positive slope.
- [ ] Worker RSS delta < 30 MB.
- [ ] CI Migrations green on 0111, up and `downgrade base`.
- [ ] `32` §14's S0 acceptance items pass, live where they say live.
- [ ] `sysLocation` of `SEN1-DC2-HA-R2-01` reads New York. The plane is back:
      `pgrep -af snmpsim`, `ss -lunp | grep :161`. `/api/air/influence` answers,
      which means spatial mode.

---

### Phase 1 — L0 data quality and L2 state-gated rules

**Goal.** Day-one value with no history. Condemn bad data, and raise named,
explainable diagnoses as derived alerts capped at MINOR. Land simulator S1 and
the collector change in the same slot, so there is one collector rebuild and one
BACnet restart.

**Platform**

| # | Task | Files | Effort | Dep |
|---|---|---|---|---|
| 1.1 | Rule framework: required metrics, gating, tolerance, set/clear dwell, **hold-on-mask**; dwell in `dcim:anomaly:acc` | P: `analytics/detectors/rules.py` | M 10 | 0.11 |
| 1.2 | DQ-1…DQ-5 with `29` §12.1 params. DQ-1 only while the endpoint is ONLINE and ingest lag < 60 s; network devices excluded. DQ-2 consumes the new BACnet quality from 1.21 | P: `analytics/detectors/quality.py` | L 14 | 1.1 |
| 1.3 | Sweep reader: incremental via the `dcim:anomaly:win` cursor (30-min backfill after a Redis loss); booleans with an 1800 s look-back and last-known value (`changelog.py:48,52`); L0 mask before L2; **yield** when `_last_lag_s > 60` | P: `analytics/sweep.py` | M 8 | 1.2 |
| 1.4 | CR-1, CR-2, CR-6; `thermal.py` constants into the catalogue | P: `detectors/rules.py`, `catalogue.py`, `services/thermal.py` | M 8 | 1.1 |
| 1.5 | CH-1, CH-4 (`metric_key=None`; heartbeat rows are not starts), CH-5 | P: `detectors/rules.py` | M 8 | 1.3 |
| 1.6 | CD-1, CD-6 | P: `detectors/rules.py` | M 5 | 1.1 |
| 1.7 | VA-1 (valves only), VA-2 | P: `detectors/rules.py`, `services/plant.py` | S 4 | 1.1 |
| 1.8 | PU-3 proof vs process (`metric_key=water_flow`), GN-1, AT-1 | P: `detectors/rules.py` | M 6 | 1.3 |
| 1.9 | RM-2, RM-3 | P: `detectors/rules.py` | M 6 | 1.4 |
| 1.10 | CT-2, CT-3, AT-1 and PL-1 (per chiller), **seeded disabled** (`29` §12.1). The campaign runner enables each for its run and restores it afterwards (`32` §13.1) | P: `detectors/rules.py` | M 6 | 1.1 |
| 1.11 | Raise/clear per `29` §9.2. Severity is `min(row, DERIVED_SEVERITY_CAP)`. `attributes.anomaly` = finding id, detector, version, diagnosis, expected, observed, threshold. Finding has `lane=alert`, `kind=process`. `clear_alarms`. **`refresh_device_alarm_state` for touched devices.** Outbox in the transaction; fanout after commit, **≤ 50 frames on `anomalies`**. `evidence.rule_evidence()` | P: `analytics/sweep.py`, `analytics/evidence.py`, `repositories/anomalies.py`, `ingest/fanout.py` | M 7 | 0.14, 1.1 |
| 1.12 | **Taxonomy entries** (`29` §9.3 names). COOLING: `crah_supply_over_setpoint`, `crah_low_delta_t`, `crah_valve_hunting`, `chiller_chw_over_setpoint`, `chiller_short_cycling`, `chiller_condenser_rising`, `chiller_low_delta_t`, `cdu_facility_starved`, `cdu_condensation_risk`, `valve_position_mismatch`, `valve_hunting`, `pump_failed_to_prove`, `tower_fan_saturated`, `tower_basin_imbalance`. ENVIRONMENTAL: `inlet_rate_of_change`, `rack_hot_spot`. POWER: `generator_fuel_loss`, `ats_transfer_rate`. Each goes into `BY_ALARM_TYPE` **and** `NON_TRAP_ALARM_TYPES`, disabled rows included | P: `app/core/alert_taxonomy.py`, `tests/test_alert_taxonomy.py` | S 2 | — |
| 1.13 | Migration **0112** `0112_the_rules_that_need_no_history.py` (`29` §12.1) | P: `alembic/versions/` | S 3 | 0.8 |
| 1.14 | API per `30` with `31`'s accepted asks: `alarm_id` filter, dismiss `reason`, `param_schema`, overrides, exclusions, `anomaly.finding.*` | P: `api/v1/anomalies.py`, `schemas/anomalies.py`, `repositories/anomalies.py` | L 14 | 1.11 |
| 1.15 | Nightly batched prune (`29` §6.3), run from `_maybe_fit_baselines` | P: `analytics/baselines.py` (prune), `repositories/anomalies.py` | S 3 | 0.12 |
| 1.16 | Frontend: evidence panel `AnomalyEvidence.tsx` for `detection=derived`; ack stays on the alarm (`31` D11); `?detection=` select on `AlarmList` | P: `features/alarms/AlarmDetail.tsx`, `AnomalyEvidence.tsx`, `AlarmList.tsx`, `api/client.ts` | M 8 | 1.14 |
| 1.17 | Frontend: **port `NumberInput`**; `AnomalyDetectors.tsx` with `ParamForm`/`OverridesTab`/`ExclusionsTab`; nav and route | P: `components/NumberInput.tsx`, `features/settings/AnomalyDetectors.tsx`, `features/settings/anomaly/*`, `SettingsLayout.tsx`, `App.tsx` | L 14 | 1.14 |
| 1.18 | Severity-cap guard; `severity_cap` in `/health` | P: `analytics/sweep.py`, `api/v1/anomalies.py` | S 2 | 1.11 |
| 1.19 | Campaigns, soak, scoring, tuning by PATCH | operator | M 10 | all |

**Collector and mappings (one rebuild)**

| # | Task | Files | Effort | Dep |
|---|---|---|---|---|
| 1.21 | **BACnet quality.** Read `Status_Flags` and `Reliability` with each present value and map them: FAULT or `Reliability ≠ NO_FAULT_DETECTED` → `BAD`; OUT_OF_SERVICE/OVERRIDDEN → `SUSPECT`. Today every sample is GOOD (`bacnet.go:347`). Also verify that the Modbus `Data_Valid`/`Reading_Valid` discretes and Redfish `Reading: null` already produce non-GOOD or no sample (`32` §4.5), and fix them if not. Without this task, simulated BACnet dropout reads as a flatline and DQ-1/DQ-2 are blind | P: `collector/internal/adapters/bacnet/bacnet.go`, `codec.go`, `bacnet_test.go`; Modbus/Redfish adapters if needed | M 8 | — |
| 1.22 | **New points' mappings.** Map chiller `Cond_Approach_Temp`/`Evap_Approach_Temp` → `approach_temp{COND\|EVAP}` and CRAH `Filter_DP` → `filter_diff_pressure` (`32` D6). Then update the embedded copy (`scripts/sync_mapping_bundle.py`), run `contracts/mapcheck.py`, rebuild with 1.21, and trigger BACnet re-discovery. No registry key, no `metric` upsert. CR-4/CH-3 stay disabled until P3 | P: `contracts/mappings/bacnet/objects.yaml`, collector embedded bundle | M 6 | S1-2, S1-5 |

**Simulator (`32` S1 + S3; one BACnet Stop → Start)**

| # | Task (`32` ID) | Effort | Dep |
|---|---|---|---|
| 1.23 | S1-1…S1-6: `Degradation` with persistence/resume; `cop`, `hx_approach` (chiller + CDU), `tower_approach`, `filter_loading` (CRAH + CDU), `pump_derate`; new points `Cond_Approach_Temp`, `Evap_Approach_Temp`, CRAH `Filter_DP` | L 33 | 0.4 |
| 1.24 | S1-7: ramp + oscillate on overrides (409 on store-published points) | M 6 | 0.3 |
| 1.25 | S1-8: **setpoints honoured by physics.** Today the chiller CHW and CDU setpoints are rewritten every tick and the CRAH setpoint is ignored. This must land before any setpoint negative control or CUSUM-reset test | L 12 | 0.4 |
| 1.26 | S1-9: weather override. S1-10: CDU `Alarm_HighSupplyTemp` coupling (one of the ~7 forced alarms with the HighTemp problem, `32` §9.2) | M 8 | 0.4 |
| 1.27 | S3-1: campaign library `S:tools/anomaly_campaigns.json` + runner `S:tools/live_campaign_anomaly.py` (`--send`, `--allow-fire`, enable-for-run, truth/wire read-back) | M 10 | 0.3-0.5 |
| 1.28 | S3-2: calibration runs, with measured windows written back into `anomaly_campaigns.json` | M 6 | 1.27 |
| 1.29 | S2-1: simulator web UI Faults tab (edit only; `run.sh` builds it) | M 10 | 0.5 |

> **Contract deviation — CD-1 margin.** `29` seeds `over_setpoint_k 1.0`. The
> display verdict uses 2.0 K (`thermal.py:177`), and the parity rule needs one
> value. Recommendation: **2.0 K** (Q12).

> **Contract deviation — the sensor-health queue has no screen until P3.** `31`
> §18 ships the triage tab in Phase 3. Until then, L0 findings are visible only
> through the API, `/health` and settings. Accept this, or pull the view forward
> by about 10 h (Q16).

> **Gaps in `32`'s library for Phase 1 detectors.**
>
> - CD-1 has no abrupt scenario. It is scored in P3 through `cdu-hx-fouling` ("then
>   CD-1 when the facility valve saturates") and only by fixture in P1.
> - VA-2 has no named scenario. `32` §13.6 offers "VA-2 via `oscillate` on the 4
>   valve devices"; this plan asks `32` to add `valve-hunting` to the library.
> - GN-1 has no ground truth until S4 `fuel_leak`. Its seed stays as `29` has it,
>   and it is scored by fixture only.
> - `rpp-trip-crah` (Phase 2, power-chain root) is defined in `32` §13.2, rooted at `MPPA-DC1-HA` (a mechanical panel, not an RPP), and needs simulator task S1-11 (`mpp_breaker_trip`).

**Unit tests**

| File | Asserts |
|---|---|
| `backend/tests/test_anomaly_quality.py` | DQ-1 fires after grace, but not while OFFLINE or lagging. DQ-2 bad-fraction rule, fed by BAD quality. DQ-3 sentinels. DQ-4 masks single spikes and opens a finding above `max_spikes_per_h`. DQ-5 fires on a frozen supply with a moving valve; silent on a frozen setpoint and on static SNMP points |
| `backend/tests/test_anomaly_rules.py` (per rule) | Fires after exactly the set dwell. Gating blocks it (not running, `running_min_s`, restart exclusion, fire pre-discharge, standby). Clears after the clear dwell. Holds under a mask. CR-1 valve split diagnosis |
| `backend/tests/test_anomaly_lkv.py` | CH-4/PU-3 rebuild from change-logged booleans; a heartbeat is not a start |
| `backend/tests/test_anomaly_detection_is_derived.py` | Every L2 raise has `detection='derived'`; no state `metric_key` |
| `backend/tests/test_anomaly_rule_parity.py` | `classify_crah`/CR-1, `plant._verdict`/VA-1 and `classify_cdu`/CD-1 each share one constant |
| `backend/tests/test_anomaly_catalogue.py` | Every row with an `alarm_type` has both taxonomy entries and its `29` §9.3 category. Alert-lane rows have an `alarm_type`. `required_metrics` ⊆ `metrics_gen.METRICS` |
| `backend/tests/test_alert_taxonomy.py` (extended) | Two-way checks pass |
| `backend/tests/test_anomaly_severity_cap.py` | No analytics raise exceeds MINOR while the cap is on |
| `backend/tests/test_anomaly_rollup_live.py` (DB) | `device_state.max_severity` follows raise and clear |
| `backend/tests/test_anomaly_fanout_cap.py` | 120 openings in one sweep → ≤ 50 frames + 1 summary |
| `backend/tests/test_anomaly_jira_policy.py` | MINOR derived alert is ineligible |
| `backend/tests/test_anomaly_api.py` | Feedback transitions; `verdict` survives `resolved` (`29` D3); `reason` required for dismiss; `param_schema` bounds; **403 for a hand-minted viewer token** on every mutation |
| `backend/tests/test_anomaly_rules_live.py` (DB) | One alarm with `source='analytics'` and `attributes.anomaly.finding_id`, with an outbox row in the same transaction |
| `backend/tests/test_anomaly_prune_live.py` (DB) | Batched prune deletes only rows past retention |
| P: `collector/internal/adapters/bacnet/bacnet_test.go` (extended) | FAULT flag → BAD; `Reliability=NO_SENSOR` → BAD; OUT_OF_SERVICE → SUSPECT; clean → GOOD. Runs against the pysnmp-style fixture pattern already in the Collector CI job |
| P: CI `mapcheck` + `sync_mapping_bundle.py --check` | The new mapping rows reference registered keys, and the embedded copy is in sync |
| S: per `32` §15 S1/S3 (`test_degradation.py`, `test_plant_override_ramp.py`, `test_setpoint_events.py`, `test_weather_override.py`, `test_forced_couplings.py`, offline campaign twins in `test_fault_campaign.py`) | Per `32` §14 |

Frontend verification uses `tsc`, the production build, and `31` §17's Phase 1
items checked live. Component tests wait for Q15.

**Campaigns** (`32` §13 names; abrupt scenarios run **3×** each, see §4)

- **Targets:**
  - `crah-coil-starve` (CR-1 starved branch; needs 0.6)
  - `valve-actuator-stuck` (VA-1)
  - `chiller-short-cycle` (CH-4)
  - `tower-cell-partial` (CH-5, CT-2 enabled for the run, CH-1)
  - `pump-proof-lost` (PU-3)
  - `cdu-condensation` (CD-6; needs 1.25)
  - `utility-flap` (AT-1 enabled for the run)
  - `crah-single-trip` (RM-3)
  - `all-chillers` (CR-1, RM-2 as diagnoses)
- **Documented expected misses:** `all-towers` and `all-cwp` for CH-5. A trip in
  about 34 s is about one poll (`32` §13.2).
- **Sensor:**
  - `crah-return-dropout` (DQ-1/DQ-2; needs 1.21)
  - `chw-supply-noisy` (DQ-4)
  - `pdu-probe-stuck` (DQ-5)
  - `valve-feedback-stuck` (VA-1 fires; its `kind` is scored in P4)
  - `crah-supply-bias`. In P1 CR-1 **will** fire falsely, because the L0 mask
    that suppresses it is DQ-6 (P3). The result is recorded, not gated.
- **Negative controls:**
  - `lead-chiller-stop` (CH-4 must not fire)
  - `hall-crahs` (no CR-*)
  - `maintenance-crah`
  - `crah-setpoint-raise` and `chw-reset` (both need 1.25)
  - `sim-restart` (DQ-1 during the gap only)
  - `fire-pre-discharge`, **only with `--allow-fire` and the user's explicit
    approval for that run**
- **Then:** `clean-soak-7d`.

**Deploy (§5).**

1. Simulator S1: BACnet Stop → Start, because new objects are built at start.
2. Collector rebuild (1.21 + 1.22), then restart the 8 fleet collectors with
   supervisor loops stopped first, then BACnet re-discovery.
3. Platform restart: 0112, detectors, API, frontend.

Seeded-on rows go live at the restart. Watch for 30 min and disable a family by
PATCH if a budget breaks.

**Exit criteria**

- [ ] `clean-soak-7d`: **0 derived alerts** that match no injection. L0 ≤ 0.01
      findings per in-scope device-day, each explained.
- [ ] Every P1 target scenario is detected in **3 of 3 runs** (recall 100 %;
      expected misses excluded and listed). Precision ≥ 95 %. **Delay is within
      the calibrated window** in `anomaly_campaigns.json`, ± one poll interval.
      Lead over L1 is reported, not gated.
      - `32` predicts a negative lead for most abrupt diagnoses (CR-1 in
        `all-chillers` follows `Alarm_HighCHWSupply`). That is correct: they
        are diagnoses, not early warnings.
      - CH-5 must lead the trip on `tower-cell-partial` whenever a trip occurs.
- [ ] Negative controls give 0 unshelved derived alerts. The setpoint controls
      give 0 after the transient.
- [ ] `crah-return-dropout` produces a DQ finding, which proves 1.21 live.
- [ ] **No analytics alarm is above MINOR.** 0 ticket-eligible outbox rows.
- [ ] Sweep **p95 < 3 s**, max < 10 s. RSS delta < 100 MB on the lock holder and
      < 20 MB on the other worker.
- [ ] Ingest within 10 % of P0. 0 slow-consumer disconnects caused by anomaly
      frames.
- [ ] `approach_temp{COND}` and `filter_diff_pressure` are arriving for every
      chiller and CRAH, and history is accruing.
- [ ] `all-chillers` first-10-min alert count is recorded (P2 gates it).
      Steady-state derived alerts ≤ 1/h.

---

### Phase 2 — L5 cooling correlator

**Goal.** One cascade produces one incident with the correct root. After the
gate, MAJOR is allowed for the incident root only.

| # | Task | Files | Effort | Dep |
|---|---|---|---|---|
| 2.1 | Cooling dependency graph: train → CHW loop (from `connection` layer `cooling`, a = plant, `core/layers.py:20-26`) → CRAH/CDU → zone (room, `repositories/thermal.py:54-56`, until P4) → server. Power hop via `services/power.py:236` and `services/impact.py:27-38` | P: `analytics/correlator.py` | L 14 | P1 |
| 2.2 | Incidents per `29` §8.5. Walk upstream within ±10 min. Rank by tier, then onset. Set `is_symptom`/`root_cause_alarm_id` **only for cooling and environmental categories**, so `alarms/correlation.py:378-429` keeps reachability | P: `analytics/correlator.py`, `repositories/anomalies.py` | L 14 | 2.1 |
| 2.3 | Context suppression: gas pre-discharge, standby, maintenance target, `lifecycle='installed'` | P: `analytics/correlator.py` | M 8 | 2.2 |
| 2.4 | L5 escalation. With confirmed impact (≥ 3 racks with RM-2/RM-3 under one root), **escalate the root's existing alarm** to MAJOR (in place, `alarms.py:326-332`). Guarded by `L5.major_enabled`, false until the gate. No new type | P: `analytics/correlator.py`, `analytics/sweep.py` | M 6 | 2.2 |
| 2.5 | Taxonomy: **none needed** (escalation reuses the root's type); the catalogue test covers it | — | — | — |
| 2.6 | Incident API + `anomaly.incident.*` within the frame budget (`30`) | P: `api/v1/anomalies.py`, `ingest/fanout.py` | M 6 | 2.2 |
| 2.7 | Frontend: `IncidentTree.tsx` in the alarm evidence panel (`31` §6.5, §8); full triage tab in P3 | P: `features/analytics/anomaly/IncidentTree.tsx`, `features/alarms/AnomalyEvidence.tsx` | M 8 | 2.6 |
| 2.8 | Add `rpp-trip-crah` (`32` §13.2) to `anomaly_campaigns.json`; depends on S1-11 `mpp_breaker_trip` | S: `tools/anomaly_campaigns.json` | S 4 | 1.27, S1-11 |
| 2.9 | Campaigns; set `major_enabled` if the gate passes and Q13 = cap | operator | M 10 | all |

**Unit tests**

| File | Asserts |
|---|---|
| `backend/tests/test_anomaly_cooling_graph.py` | Tiers. A CRAH with no cooling edge joins its room. Header grouping |
| `backend/tests/test_anomaly_correlator.py` | Chiller trip → 1 incident rooted at the chiller, members as symptoms. RPP trip → RPP root. Fire → no CRAH root. Maintenance excluded. Onset tie-break. A finding at +9 min attaches; one at +11 min is new |
| `backend/tests/test_anomaly_correlation_coexist.py` | Reachability marks unchanged. `endpoint_unreachable`/`telemetry_stale` never touched (`correlation.py:38-44`) |
| `backend/tests/test_anomaly_promotion.py` | 2 racks → nothing. 3 racks → MAJOR only when `major_enabled`. Idempotent |
| `backend/tests/test_anomaly_api.py` (extended) | Incident endpoints; viewer-token 403 |

**Campaigns:**

- `all-chillers` (one incident at the chiller plant)
- `all-chwp` (root = pump train)
- `all-towers`, `all-cwp`
- `hall-crahs` (root = CRAH group)
- `tower-cell-partial` (root = tower)
- `crah-single-trip` (room-level root = that CRAH)
- `crah-coil-starve`
- `cdu-leak` (root = CDU)
- `rpp-trip-crah` (needs S1-11)
- negative controls `lead-chiller-stop`, `maintenance-crah`, and
  `fire-pre-discharge` (approval required)

**Deploy.** One platform deploy after the P1 soak. No migration, no simulator
change.

**Exit criteria**

- [ ] `all-chillers`, `all-chwp`, `all-towers`, `all-cwp`, `hall-crahs`,
      `tower-cell-partial`, `cdu-leak` and `crah-coil-starve` (and
      `rpp-trip-crah`) each give **exactly one incident with the
      correct root** in 3 of 3 runs.
- [ ] **Compression ≥ 10:1** on `all-chillers` and `hall-crahs`.
- [ ] **≤ 10 non-symptom alarms and derived alerts in the first 10 min** of each
      cascade (EEMUA 191).
- [ ] `fire-pre-discharge`: 0 CRAH-rooted incidents. `maintenance-crah`: 0
      incidents. `lead-chiller-stop`: 0 incidents beyond the L1 state change.
- [ ] Each incident opens within 2 sweeps of its root's first signal. Sweep p95
      < 3 s.
- [ ] **After all of the above:** with `major_enabled`, one `all-chillers` rerun
      gives exactly one ticket-eligible outbox row (the root).

---

### Phase 3 — L3 residuals and baselines

**Goal.** Catch slow degradation as advisories, within a budget, and give them
their triage screen.

**Preconditions.**

- ≥ 14 days of clean history since P0. Pre-fix DC2 history and the C1 step are
  excluded.
- ≥ 14 days of the new points' history since P1, for CR-4 and CH-3.

| # | Task | Files | Effort | Dep |
|---|---|---|---|---|
| 3.1 | Nightly fit over `telemetry_1h`/`1d`. Refuse thin buckets (`sample_count` < 50 %). Subtract exclusions, maintenance windows and ledger windows. **Freeze the refit on an open process finding.** Limit coefficient change to ≤ 10 %/night. Per-batch transactions under DCIMB. Prune superseded fits after 30 d | P: `analytics/baselines.py` | L 20 | P0 |
| 3.2 | Residual framework: residual/σ, EWMA, two-sided CUSUM reset on a setpoint event (now real, 1.25), checkpoint and restore per `29` §10.3-10.4 | P: `analytics/detectors/residual.py` | L 14 | 0.10 |
| 3.3 | CH-2, CH-3, CT-1, CT-4, PU-1, PU-2, PU-4, CR-3, CR-4, CD-2, CD-3, CD-4, CD-5, SV-1, SV-2, PD-1, PD-2, UP-1, UP-2, TX-1, and DQ-6 (`29` D1) | P: `analytics/detectors/residual.py`, `detectors/quality.py` | L 30 | 3.1, 3.2 |
| 3.4 | Migration **0114** `0114_a_residual_needs_a_baseline.py` (`29` §12.2) | P: `alembic/versions/` | S 3 | 0.8 |
| 3.5 | **Taxonomy entries for every L3 promotion `alarm_type`.** `chiller_efficiency_degraded` (CH-2, `29`), plus proposed names for `29` to confirm: `tower_approach_degraded` (CT-1), `pump_curve_degraded` (PU-1), `pump_efficiency_degraded` (PU-2), `crah_fan_creep` (CR-3), `crah_filter_loading` (CR-4), `chiller_approach_degraded` (CH-3), `cdu_approach_degraded` (CD-2), `cdu_filter_loading` (CD-3). All COOLING, in both files | P: `alert_taxonomy.py`, `tests/test_alert_taxonomy.py` | S 2 | 3.4 |
| 3.6 | `GET /expected` with `interval`, multi-metric, no-baseline `reason` (`31` D7) | P: `api/v1/anomalies.py`, `repositories/anomalies.py` | M 6 | 3.1 |
| 3.7 | `impact_kw` estimates | P: `analytics/evidence.py` | M 6 | 3.3 |
| 3.8 | Advisory budget: chatter collapse (`29` §8.2), 24 h stale flag, daily cap that auto-disables a detector | P: `analytics/sweep.py`, `repositories/anomalies.py` | M 5 | 3.3 |
| 3.9 | Frontend: `TimeChart` optional `band`/`marks`/`width` + `components/band.ts` `bandRuns()`. Output with no band must stay byte-identical. `Plot.tsx` untouched | P: `components/TimeChart.tsx`, `components/band.ts` | M 8 | 3.6 |
| 3.10 | Frontend: `DeviceHistory.tsx` findings line, band mode, **"Under watch" panels** for open-finding metrics missing from `chartGroups.ts` | P: `features/devices/DeviceHistory.tsx` | M 8 | 3.9 |
| 3.11 | Frontend: triage tab (`AnomalyView` Incidents/Findings/Sensor health, `AnomalyDetail`, `anomaly/*`, `anomaly.css`, topic `anomalies`, `usePaged`) | P: `features/analytics/Analytics.tsx`, `AnomalyView.tsx`, `AnomalyDetail.tsx`, `anomaly/*`, `anomaly.css` | L 20 | 3.6 |
| 3.12 | Offline replay with VUS-PR | P: `backend/scripts/anomaly_eval.py`, `analytics/evaluation.py` | M 10 | 3.3 |
| 3.13 | Enable CR-4 and CH-3 by PATCH. They are seeded disabled in 0114 (`29` D9, D10), and their points have been live since P1 | operator | S 1 | 1.22 |
| 3.14 | *(Optional, not in total.)* `32` S4-x additive kinds, highest value first: `hunting`, `loop_leak`, `server_thermal`, `bypass`, `fuel_leak`, `battery_aging`, `xfmr_cooling`, `server_fan_wear`, and CRAH coil + valve-driven CHW flow | S: per `32` | L ~30 | 1.23 |
| 3.15 | Gradual campaigns and soak | operator | M 12 | all |

> **Contract deviation — seeded on with no ground truth.** `29` §12.2 seeds
> CD-5, SV-1, UP-1 and TX-1 enabled. Until S4 (3.14) gives them honest physics
> (`32` §13.6), this plan recommends `enabled = false`. If they stay on, the P3
> deploy PATCHes them off before the first fit makes them live (Q17). CD-5 has
> an interim scenario (`cdu-slow-leak`, a pressure ramp). It tests plumbing
> rather than physics and is scored as such.

**Unit tests**

| File | Asserts |
|---|---|
| `backend/tests/test_anomaly_baselines.py` | Thin buckets refused. Exclusions removed. Freeze on an open process finding. Coefficient cap. Deterministic fit. The daily guard survives a Redis flush |
| `backend/tests/test_anomaly_baseline_lock_live.py` (DB) | Session lock held across batches and released in `finally` and on disconnect. No transaction longer than one batch |
| `backend/tests/test_anomaly_residual.py` | CUSUM catches 0.1 σ/h within bounded hours. A setpoint event resets it. Checkpoint and restore round-trip. Reset after 6 h or a version/baseline change |
| `backend/tests/test_anomaly_detectors_l3.py` (parametrised) | Each detector fires on its synthetic fault, stays silent on the clean twin, and respects its gating |
| `backend/tests/test_anomaly_budget.py` | Chatter, stale flag, daily cap |
| `backend/tests/test_anomaly_expected_api.py` | Shape, `interval`, multi-metric, 200 + `reason` when no baseline |
| `backend/tests/test_anomaly_catalogue.py` (extended) | Promotion types carry both taxonomy entries |

**Campaigns** (fast durations from `32` §13.3; realistic variants are optional,
Q5):

- **Gradual:**
  - `chiller-cop-decay` (CH-2; CH-3 must **not** fire)
  - `chiller-condenser-fouling` (CH-3, CH-2)
  - `tower-fill-fouling` (CT-1, then CT-2 enabled for the run; root = tower)
  - `crah-filter-loading` (CR-4, CR-3)
  - `cdu-strainer-loading` (CD-3, CD-4)
  - `cdu-hx-fouling` (CD-2, then CD-1)
  - `chwp-impeller-wear` (PU-1, PU-2)
  - `pump-bearing-heat` (PU-4)
  - `tower-fan-imbalance` (CT-4)
  - `tower-basin-leak` (CT-3 enabled for the run)
  - `cdu-slow-leak` (CD-5, interim)
  - `pdu-imbalance` (PD-2)
- **Sensor** (detected in P3; `kind` scored in P4):
  - `rack-probe-drift`
  - `chw-flow-meter-bias` (DQ-6; PL-2 not seeded)
  - `bmc-inlet-bias`
  - `tower-wetbulb-bias`
  - `cdu-filter-dp-drift`
  - `ups-battery-probe-drift`
  - `crah-supply-bias` (**now gated**: DQ-6 must mask CR-1)
- **Negative controls:** `heatwave`, `crah-setpoint-raise`, `chw-reset`
- **Then:** `clean-soak-7d`

**Deploy.** One platform deploy: 0114, detectors, `/expected`, frontend. No
simulator change, unless S4 is taken. L3 rows stay inert until the first nightly
fit, and two nights of fits are inspected before relying on findings.

**Exit criteria**

- [ ] Gradual campaigns: **recall ≥ 80 %**, precision ≥ 70 %. Detection inside
      the calibrated window.
- [ ] **Median lead over L1 ≥ 2 h** where L1 fires. "No L1" is recorded where it
      does not.
- [ ] `clean-soak-7d`: **≤ 0.05 false findings per device-day**; **≤ 30
      advisories/day estate-wide**; ≤ 10/day per detector.
- [ ] `heatwave`: 0 CH-2 and 0 CT-1 process findings. `chw-reset`: the COP
      improvement is excluded and produces no CH-2.
- [ ] `crah-supply-bias`: CR-1 is masked (0 alerts).
- [ ] Nightly fit < 10 min, transient memory < 300 MB. Sweep p95 < 5 s.
      Lock-holder RSS delta < 150 MB.
- [ ] `31` §17 Phase 3 items hold live, including "a chiller with an open CH-2
      shows an Under watch · COP panel with a band".

---

### Phase 4 — L4 peer, spatial and zones

**Goal.** Name the CRAH zone, not just the hall, and tell a lying sensor from a
failing machine.

**Precondition.** ≥ 28 days of `spatial` history. The ledger's CRAH stops serve
as natural experiments.

| # | Task | Files | Effort | Dep |
|---|---|---|---|---|
| 4.1 | CR-5; RM-4; SV-3 **anchored to the CDU** (`29` §12.3; the room-wide variant is L5 evidence) | P: `analytics/detectors/peer.py` | M 12 | P3 |
| 4.2 | Influence matrix: per-room ridge regression of rack inlet on CRAH supply and state, using ledger stops as experiments, stored as RM-4's room-scoped `influence` baseline and labelled an estimate. Optional exp(−d/5 m) prior once the 0099 x/y import is verified | P: `analytics/baselines.py`, `detectors/peer.py` | L 20 | 3.1 |
| 4.3 | `kind` discrimination from peer and DQ-6 balance residuals; optional per-class PCA SPE with a > 50 % single-variable rule | P: `detectors/peer.py`, `detectors/quality.py` | L 16 | 4.1 |
| 4.4 | Zone roots in the correlator (`L5.use_influence`), falling back to room | P: `analytics/correlator.py` | M 8 | 4.2 |
| 4.5 | Migration **0115** `0115_a_unit_is_judged_against_its_siblings.py` | P: `alembic/versions/` | S 2 | 0.8 |
| 4.6 | Taxonomy: **none needed** (L4 is advisory; SV-3 feeds L5) | — | — | — |
| 4.7 | Influence scoring against `GET /api/air/influence` (built in 0.7) | P: `analytics/evaluation.py`, `scripts/anomaly_eval.py` | M 6 | 4.2 |
| 4.8 | Frontend: zone shown in `IncidentTree`/`AnomalyDetail`; sensor-health count | P: `features/analytics/anomaly/IncidentTree.tsx`, `AnomalyView.tsx` | M 8 | 4.4 |
| 4.9 | Campaigns | operator | M 10 | all |

**Unit tests**

| File | Asserts |
|---|---|
| `backend/tests/test_anomaly_peer.py` | Robust z ignores 1 outlier in 7; refuses with < 4 running; SV-3 thresholds |
| `backend/tests/test_anomaly_influence.py` | Recovers a synthetic exp(−d/5) matrix (ρ ≥ 0.9); stable with collinear CRAHs |
| `backend/tests/test_anomaly_kind.py` | Bias on one probe → `sensor`; degraded CRAH warming 6 racks → `process`; conflicting evidence → `undetermined` |

**Campaigns:**

- **Zone:** `crah-single-trip`, once per CRAH in one hall (7 runs).
- **`kind`, sensor cases:** `crah-supply-bias`, `crah-supply-bias-control`
  (diagnosis reported, not gated, per `32`), `rack-probe-drift`,
  `bmc-inlet-bias`, `tower-wetbulb-bias`, `cdu-filter-dp-drift`,
  `ups-battery-probe-drift`, `valve-feedback-stuck`, `pump-proof-lost`.
- **`kind`, process cases:** `crah-filter-loading`, `cdu-hx-fouling`.
- **SV-3:** `cdu-leak`.

**Deploy.** One platform deploy, with `use_influence=false` until 4.7 passes.
No simulator change.

**Exit criteria**

- [ ] `crah-single-trip`: fitted top-1 CRAH = ground truth for ≥ 80 % of racks
      with a true weight > 0.2. Spearman ρ ≥ 0.7 per room.
- [ ] Zone roots name the stopped CRAH in ≥ 6 of 7 runs, beating room-level
      grouping.
- [ ] **`kind` accuracy ≥ 85 %** across the scenarios listed above, excluding
      `crah-supply-bias-control`.
- [ ] Phase 3 soak figures do not regress. Combined sweep p95 < 5 s.

---

### Phase 5 — L6 explainer (optional)

**Goal.** An optional, labelled narrator over facts the platform has already
established. It has no authority.

| # | Task | Files | Effort | Dep |
|---|---|---|---|---|
| 5.1 | Full evidence bundle (`29` §7.4) + template summary + bundle cache | P: `analytics/evidence.py` | M 10 | P2 |
| 5.2 | `explainer.py` none/cloud/local. JSON schema at temperature 0. Validator with template fallback. Explain lock. Audit + `incident.explanation`. Names and ticket text treated as data | P: `analytics/explainer.py`, `repositories/anomalies.py` | L 16 | 5.1 |
| 5.3 | `POST /incidents/{id}/explain` (409 under `none` or while locked) + `/explain/feedback` (`31` D8) | P: `api/v1/anomalies.py` | S 4 | 5.2 |
| 5.4 | Jira body drafted from the bundle, template first | P: `app/integrations/jira/` | M 6 | 5.1 |
| 5.5 | Frontend `AiExplanation.tsx`: placed below the evidence in `AnomalyDetail`, labelled, with thumbs; hidden when `/health.explainer = none`. The alarm evidence panel carries only an "AI explanation available" link into it (`31` §8.2: the 380 px pane cannot fit the section) | P: `features/analytics/anomaly/AiExplanation.tsx`, `AnomalyDetail.tsx`, `features/alarms/AnomalyEvidence.tsx` | M 8 | 5.3 |
| 5.6 | Red-team fixtures | P: tests | M 5 | 5.2 |
| 5.7 | Regression run of the P1–P4 scoring with `none` | operator | S 5 | all |

**Unit tests**

| File | Asserts |
|---|---|
| `backend/tests/test_anomaly_evidence.py` | Deterministic hash; every item has an ID; size cap |
| `backend/tests/test_anomaly_explainer_validator.py` | Rejects unknown IDs, unknown devices and schema violations, then falls back; accepts valid output |
| `backend/tests/test_anomaly_explainer_readonly.py` | Static import check: no write clients, no alarm mutators |
| `backend/tests/test_anomaly_prompt_injection.py` | Planted instructions change neither the schema nor the cited IDs |
| `backend/tests/test_anomaly_api.py` (extended) | 409s; audit row under mock `local`; feedback; viewer-token 403 |

**Campaigns.** Replay the stored P2–P4 incidents plus the red-team fixtures.

**Deploy.** One platform deploy with `none`. Switching it on is Q2 and requires a
restart.

**Exit criteria**

- [ ] P1–P4 criteria hold with `none`.
- [ ] Validator rejects < 10 % on replay, and every rejection falls back.
- [ ] 0 actuation paths. `cloud` egress is the bundle only (captured in a test).
- [ ] Explain p95 < 20 s and never blocks a sweep. Thumbs are recorded.

---

### 3.7 Consolidated detector parameter table

These are **starting values**. Where `29` §12 seeds a parameter, `29` is the
record and this table repeats it. Dwell is set/clear in seconds. "On" and "off"
are seed states. "For run" means the runner enables the detector during its
scenario only. All analytics raises are MINOR while the P1–P2 cap is active.

| ID | Ph | Layer | Device types | Signals (`metric{instance}`) | Gating | Threshold / tolerance (start) | Dwell | Lane | Sev | Kind | alarm_type | Seed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DQ-1 | 1 | L0 | crah, chiller, cooling_tower, pump, cdu, valve, sensor, pdu, ups, transformer, generator, server (not network) | any required metric | endpoint ONLINE, others delivering; ingest lag < 60 s | no sample within 3 × poll, clamped 300–10800 s (`staleness.py:36-56`) | immediate / 2 good | advisory | — | sensor | — | on |
| DQ-2 | 1 | L0 | same | `quality` (BACnet via 1.21) | — | ≥ 50 % non-GOOD over 1800 s, ≥ 5 samples | 1800 window | advisory | — | sensor | — | on |
| DQ-3 | 1 | L0 | same | value | — | per-metric bounds; sentinels −40, −127, 85, ±327.67 | 3 samples | advisory | — | sensor | — | on |
| DQ-4 | 1 | L0 | same | value | — | Hampel w = 11, 5 × MAD; single spikes masked; finding at > 6/h | 1 h | advisory | — | sensor | — | on |
| DQ-5 | 1 | L0 | same | value + coupled variable (`29` §12.1) | coupled range ≥ 5 % / 0.5 K | zero variance over 20 samples | 20 samples | advisory | — | sensor | — | on |
| DQ-6 | 3 | L0 | sensor, pdu (+ CDU/chiller balance) | peer + balance residuals | steady | \|residual\| > 15 %; CUSUM k = 0.5σ, h = 5σ | hours | advisory | — | sensor | — | on (0114) |
| CR-1 | 1 | L2 | crah | `supply_air_temp`, `air_setpoint_temp`, `valve_position_pct{CHW}`, `equipment_state{Unit_Running}` | running ≥ 600; no fire pre-discharge; not standby; not DQ-masked | > setpoint + 3.0 K (`thermal.py:52`), clear at 2.0 K; valve ≥ 95 % → CHW-starved, else control fault | 900 / 300 | alert | MINOR | process | `crah_supply_over_setpoint` | on |
| CR-2 | 1 | L2 | crah | `return_air_temp`, `supply_air_temp`, `fan_speed_pct` | running ≥ 600; fan ≥ 30 % | return − supply < 4 K, clear at 5 K | 900 / 600 | alert | MINOR | process | `crah_low_delta_t` | on (no sim truth until S4 `bypass`) |
| CR-3 | 3 | L3 | crah | `fan_speed_pct` ~ room IT kW | running; steady load | residual CUSUM k = 0.5σ, h = 5σ | hours | advisory | — | undetermined | `crah_fan_creep` (proposed) | on |
| CR-4 | 3 | L3 | crah | `filter_diff_pressure` / (fan/100)² (new point, 1.22) | running | slope CUSUM; > 10 %/week | days | advisory | — | undetermined | `crah_filter_loading` (proposed) | off → PATCH on (3.13) |
| CR-5 | 4 | L4 | crah (hall siblings) | `supply_air_temp` − `air_setpoint_temp` | ≥ 4 siblings running | robust z > 3.5 | 1800 | advisory | — | sensor/process | — | on (0115) |
| CR-6 | 1 | L2 | crah | `valve_position_pct{CHW}` | running | ≥ 6 reversals of ≥ 5 pp in 1800 | 1800 / 1800 | alert | WARNING | process | `crah_valve_hunting` | on (truth: S4 `hunting`) |
| CH-1 | 1 | L2 | chiller | `water_supply_temp{CHW}`, `water_setpoint_temp{CHW}`, `compressor_load_pct`, `equipment_state{Chiller_Running}` | running ≥ 900; restart exclusion 480 | > setpoint + 1.0 K, clear at 0.5 K, while load < 95 % | 900 / 300 | alert | MINOR | process | `chiller_chw_over_setpoint` | on |
| CH-2 | 3 | L3 | chiller | `cop` ~ OLS(load, load², `water_supply_temp{COND}` or wet bulb) | running ≥ 900; steady | residual CUSUM k = 0.5σ, h = 5σ (vendor rule: investigate at kW/ton +10 %) | hours | advisory | — | undetermined | `chiller_efficiency_degraded` | on |
| CH-3 | 3 | L3 | chiller | `approach_temp{COND}`, `{EVAP}` (new points, 1.22) | running; steady | > commissioning baseline + 3 K (vendor rule of thumb) or CUSUM | hours | advisory | — | undetermined | `chiller_approach_degraded` (proposed) | off → PATCH on (3.13) |
| CH-4 | 1 | L2 | chiller | `equipment_state{Chiller_Running}` (last-known value, 1800) | — | > 3 starts in 3600; `metric_key=None` | immediate / 3600 | alert | MINOR | process | `chiller_short_cycling` | on |
| CH-5 | 1 | L2 | chiller | `water_return_temp{COND}`, `water_pressure{COND}` | running | rise > 0.3 K/min projected to `trip_c` 42 within 600 (simulator value; per site) | 2 samples / 600 | alert | MINOR | process | `chiller_condenser_rising` | on |
| PL-1 | 1 | L2 | chiller (per chiller) | CHW ΔT, `water_flow{CHW}` | steady load | ΔT < 0.6 × 5.5 K while flow rises | 1800 / 900 | alert | MINOR | process | `chiller_low_delta_t` | off (truth: S4 CRAH coil) |
| PL-2 | — | L3 | — | plant balance | — | needs a loop entity | — | — | — | — | — | not seeded |
| CT-1 | 3 | L3 | cooling_tower | (`water_supply_temp{COND}` − `outdoor_wet_bulb_temp`) ~ load, wet bulb | cell running | CUSUM k = 0.5σ, h = 5σ | hours | advisory | — | undetermined | `tower_approach_degraded` (proposed) | on |
| CT-2 | 1 | L2 | cooling_tower | `fan_speed_pct`, `water_supply_temp{COND}`, `outdoor_wet_bulb_temp`, `equipment_state{Fan_Status}` | running | fan ≥ 98 % and approach rising > 0.5 K/h | 1200 / 600 | alert | MINOR | process | `tower_fan_saturated` | off (for run) |
| CT-3 | 1 | L2 | cooling_tower | `basin_level_pct`, `makeup_water_flow` | — | falling > 2 %/h while make-up > 0 | 1800 / 1800 | alert | MINOR | process | `tower_basin_imbalance` | off (for run) |
| CT-4 | 3 | L3 | cooling_tower | `vibration` (hour-of-week profile) | running | EWMA λ = 0.1 > profile + 4σ | hours | advisory | — | undetermined | — | on |
| PU-1 | 3 | L3 | pump | `water_flow`, `water_diff_pressure` at `pump_speed_pct` | running ≥ 600 | curve residual CUSUM | hours | advisory | — | undetermined | `pump_curve_degraded` (proposed) | on |
| PU-2 | 3 | L3 | pump | `power_draw` ~ speed³ | running ≥ 600 | residual > 10 %, CUSUM | hours | advisory | — | undetermined | `pump_efficiency_degraded` (proposed) | on |
| PU-3 | 1 | L2 | pump | `water_flow`, `equipment_state{Run_Status}`, `pump_speed_pct` | — | proof vs process (`29` D8): status 0 with speed > 10 % or flow > 5 % of mean, or status 1 with flow < 5 % after 60; `metric_key=water_flow` | 2 / 3 samples | alert | MINOR | process (`pump-proof-lost` is sensor) | `pump_failed_to_prove` | on |
| PU-4 | 3 | L3 | pump | `motor_temp` ~ `power_draw` | running | residual CUSUM | hours | advisory | — | undetermined | — | on |
| VA-1 | 1 | L2 | valve only | `valve_position_pct{MEASURED}` vs `{COMMANDED}` | settled 120 | \|cmd − fb\| > 10 pp (`plant.py:54`), clear at 5 | 600 / 300 | alert | MINOR | process / sensor (`valve-feedback-stuck`) | `valve_position_mismatch` | on |
| VA-2 | 1 | L2 | valve | `valve_position_pct{MEASURED}` | — | ≥ 6 reversals of ≥ 5 pp in 1800 | 1800 / 1800 | alert | WARNING | process | `valve_hunting` | on |
| CD-1 | 1 | L2 | cdu | `water_supply_temp{""}`, `valve_position_pct{FACILITY_CHW}`, `water_setpoint_temp`, `equipment_state{Unit_Running}` | pumps running | valve ≥ 95 % and TCS > setpoint + margin (**2.0 K recommended**; `29` has 1.0) | 300 / 300 | alert | MINOR | process | `cdu_facility_starved` | on |
| CD-2 | 3 | L3 | cdu | `approach_temp` ~ `thermal_load` | running | CUSUM (display stays 6 K, `thermal.py:184`) | hours | advisory | — | undetermined | `cdu_approach_degraded` (proposed) | on |
| CD-3 | 3 | L3 | cdu | `filter_diff_pressure` / flow² | running | slope CUSUM (display stays 55 kPa) | days | advisory | — | undetermined | `cdu_filter_loading` (proposed) | on |
| CD-4 | 3 | L3 | cdu | `pump_speed_pct` ~ `water_flow` | running | residual CUSUM | hours | advisory | — | undetermined | — | on |
| CD-5 | 3 | L3 | cdu | `water_pressure` | running | decay-slope CUSUM | days | advisory | — | undetermined | — | `29` on; **recommend off** until S4 `loop_leak` |
| CD-6 | 1 | L2 | cdu | `water_supply_temp{""}` vs room-max dew point (`ashrae.py:120`) | pumps running | margin < 2 K, clear at 3 K | 600 / 600 | alert | MINOR | process | `cdu_condensation_risk` | on |
| RM-1 | — | L1 | — | — | — | existing ASHRAE envelope rules | — | alarm | — | — | existing | exists |
| RM-2 | 1 | L2 | sensor, pdu | `inlet_temperature`, `ambient_temperature` | ≥ 3 samples in 900 | \|rate\| > 20 K/h (`ashrae.py:68`) | 3 samples / 900 | alert | MINOR | process | `inlet_rate_of_change` | on |
| RM-3 | 1 | L2 | sensor, pdu | `inlet_temperature` | ≥ 5 room sensors | > room p90 + 3 K (`thermal.py:44,48`), clear at 2 K | 900 / 600 | alert | MINOR | process | `rack_hot_spot` | on |
| RM-4 | 4 | L4 | sensor, pdu | inlet vs aisle peers / influence-expected | spatial | robust z > 3.5 | 1800 | advisory | — | sensor/process | — | on (0115) |
| SV-1 | 3 | L3 | server | `cpu_temperature` ~ (`cpu_utilization`, `inlet_temperature`) per model | powered on | 30-min EWMA residual > 3σ | 1800 | advisory | — | undetermined | — | `29` on; **recommend off** until S4 `server_thermal` |
| SV-2 | 3 | L3 | server | `fan_speed` ~ (inlet, cpu) | powered on | as SV-1 | 1800 | advisory | — | undetermined | — | on (truth: S4 `server_fan_wear`) |
| SV-3 | 4 | L4 | cdu (anchor) | fraction of the loop's servers with SV-1/SV-2 > 2σ | — | ≥ 20 % and ≥ 5 servers | 600 | advisory (L5 evidence) | — | process | — | on (0115) |
| PD-1 | 3 | L3 | pdu, rpp, energy_monitor | `power_draw` ~ Σ server `cpu_utilization` | — | residual > 5 % sustained | 1800 | advisory | — | undetermined | — | on |
| PD-2 | 3 | L3 | pdu, rpp, energy_monitor | `phase_imbalance_pct` (hour-of-week profile) | — | EWMA > profile + 3σ | 1800 | advisory | — | undetermined | — | on |
| UP-1 | 3 | L3 | ups | `battery_temperature` ~ room `ambient_temperature` | — | residual CUSUM | hours | advisory | — | undetermined | — | `29` on; **recommend off** until S4 `room_hvac_loss` |
| UP-2 | 3 | L3 | ups | `battery_runtime` ~ `load_pct` | on mains | residual < −15 % | hours | advisory | — | undetermined | — | on (truth: S4 `battery_aging`) |
| TX-1 | 3 | L3 | transformer | `oil_temperature` vs first-order model (τ ≈ 3 h) | — | residual > 3σ | hours | advisory | — | undetermined | — | `29` on; **recommend off** until S4 `xfmr_cooling` |
| GN-1 | 1 | L2 | generator | `fuel_level_pct`, `equipment_state{Engine_Running}` | not running ≥ 1800 | drop ≥ 1.0 % per 3600; rises ignored | 1800 / 1800 | alert | MINOR | process | `generator_fuel_loss` | on (fixture only until S4 `fuel_leak`) |
| AT-1 | 1 | L2 | ats | `transfer_count` | — | > 2 in 24 h | immediate / 86400 | alert | MINOR | process | `ats_transfer_rate` | off (for run) |
| L5 | 2 | L5 | — | findings + cooling alarms | context suppression | ±10 min; escalate the root's alarm at ≥ 3 racks | — | escalates the root's existing alarm | MINOR → MAJOR when `major_enabled` | process | (the root's type) | on (switch row) |

The "proposed" L3 promotion types follow `29` §9.3's pattern and need `29`'s
confirmation. RM-2 on 120 s PDU probes has an effective window of ≥ 4 min; that
is an aliasing limit, not a defect. Fire panels and ASDs get no detector, and
fire is context only.

> **Contract deviation (minor) — L5 switch row.** `29` seeds no L5 row. This
> plan wants a catalogue row `L5` (layer 5, no device types, no `alarm_type`)
> holding `enabled`, `major_enabled` and `use_influence`, so that every rollback
> is the same PATCH. It goes in `29`'s 0112 seed, or `29` names an alternative.

---

## 4. Evaluation harness

**Where.** Pure functions live in `P:backend/app/analytics/evaluation.py` and
are tested in CI against fixtures. The CLI is `P:backend/scripts/anomaly_eval.py`
with three commands: `score --campaign`, `soak --days 7` and `replay --from --to`.

**Inputs:**

- **The scenario expectations** come from `S:tools/anomaly_campaigns.json`: the
  single source both repos read (`32` §13.1). That file lists the expected
  detectors, `kind`, calibrated windows, expected misses, `must_not` sets and
  enable-for-run detectors.
- **The ledger** is `S: GET /api/faults/ledger`, at 127.0.0.1:8001, authenticated
  as in `tools/_live_api.py`. Its fields include `campaign` (`"<scenario>/<run_id>"`),
  `device_id`, `expected_kind` and `context` (`32` D5).
- **Platform state:** findings and incidents from `/api/v1/anomalies/*`, and
  derived and L1 alarms including cleared history.

**Outputs.** A report at `var/anomaly-eval/<UTC-date>-<scenario>.json`, plus one
row per (detector, version, scenario) appended to `var/anomaly-eval/history.csv`.

**Matching.** A run detects its injection when a finding or derived alert:

- opens within the scenario's calibrated window from onset (the ledger
  `start`), with a tolerance of ± one poll interval of the slowest required
  metric;
- names the root device, or belongs to an incident whose root is that device;
- comes from a detector in the scenario's `expected` set;
- from P4 onward, carries the `kind` that matches `expected_kind`.

Repeated raises of one (detector, device, instance) episode count once.

**Repeatability.** Runs are statistically repeatable, not bit-exact. Stable
hashing fixes the offsets, but the plant walks and server load are stochastic
(`32` §9.3). So:

- each abrupt scenario runs **3×**, and a 100 % recall gate means 3 of 3;
- each gradual scenario runs once, backed by its offline twin in
  `S:tests/test_fault_campaign.py`;
- delays are reported as the median and range across runs, never as a single
  run's value.

**False findings.** A finding counts as false when any of these holds:

- it opens on a clean soak, unless an operator labels it a real simulator
  artefact (that label becomes a simulator ticket, not a pass);
- during a campaign, it matches no injection and is not absorbed as a symptom of
  a correctly rooted incident;
- it names the wrong root, which counts as both a miss and a false finding;
- it appears in the scenario's `must_not` set.

Ledger windows tagged `context` (for example `sim_change`, `resume`, or a
protocol stop/start) are excluded from both numerator and denominator.

**Figures** per detector and version:

| Figure | Definition |
|---|---|
| Event recall | Detected injections ÷ injections |
| Event precision | True detections ÷ all findings |
| Detection delay | Median and p90 time from onset to detection |
| Lead over L1 | Time from detection to the first L1 alarm on the same chain; "No L1" when none fires. Negative leads are expected for abrupt diagnoses (`32` §13.2) |
| False per device-day | Measured on the soak |
| Diagnosis accuracy | Root **and** `kind` both correct |
| Compression ratio | Raw findings ÷ incidents |
| Advisory budget | Advisories per day |
| Operator precision | `confirmed / (confirmed + dismissed)` from `anomaly_finding.verdict` (`29` D3). A detector below 50 % over 30 findings is reviewed |

VUS-PR is computed in `replay` only. Point-adjusted F1 is not computed.

**Cadence.** The runner calls `score` at the end of every scenario. `soak` runs
daily during a soak, either by hand or from an opt-in WSL cron (Q10). `replay`
runs on every L3/L4 parameter change. CI runs the fixtures only.

---

## 5. Deploy procedure (every phase)

Each phase is one platform deploy. When the phase includes simulator or
collector work, those go in the same slot in the order shown.

**Before**

1. No campaign or soak is running. No Timescale job storm. Swap is not climbing
   (check `//wsl.localhost/Ubuntu/proc/meminfo` and `/proc/pressure/memory`).
2. Run `XINFO GROUPS telemetry.v1` twice, 60 s apart. Lag must be ≈ 0 with a
   slope ≤ 0. If one worker is pinned at `pending = 200`, fix the two-worker
   condition first.
3. Gates pass:
   - `ruff check app tests`;
   - `pytest`, on Windows, or the narrow module set if run inside WSL;
   - `alembic upgrade head`/`downgrade base` on a throwaway DB;
   - `npm run build`;
   - for collector work, `go vet`/`go test` and `sync_mapping_bundle.py --check`.
4. The user commits backend, docs, collector and simulator. The frontend is
   committed and pushed, and CI is checked over the REST API until green.

**Simulator** (only in phases that change it: 0 and 1, plus 3 if S4 is taken)

5. Run `git pull --ff-only` in `~/DCIM/Datacenter_Network_Simulator` as the
   user. Never copy files across.
6. If the topology fingerprint changed (P0 C2), run SNMP Stop → Regenerate →
   Start. If new BACnet objects were added (P1 S1-2/S1-5), run BACnet Stop →
   Start.
7. Restart headless with `DCIM_SIM_AIR_MODEL=spatial` and `DCIM_SIM_SEED` (in
   `auth.env`). Then **start every protocol again**, with SNMP on its configured
   port. Verify with `pgrep -af snmpsim`, `ss -lunp | grep :161` and the
   `.snmprec` mtimes. Allow about 2 min for plant stage-up.
8. Ledger any one-time step (`context: sim_change`) and post its baseline
   exclusion.

**Collector** (only in Phase 1)

9. Rebuild the Go collector (1.21 + 1.22).

**Platform**

10. Stop the collector supervisor loops, then the collectors, then
    uvicorn/ingest/vite. Run `git pull --ff-only`, then
    `setsid -f nohup ./scripts/dev.sh > var/log/dev.log 2>&1 < /dev/null`.
    `dev.sh` runs alembic (`:123`) and starts the rebuilt collectors.
11. Verify:
    - `alembic current` shows the phase head;
    - exactly one of each process, 2 ingest workers, and each collector once;
    - **`grep "ingest tick failed" var/log/dev.log` is empty**;
    - `/anomalies/health` shows a sweep within `2 × SWEEP_S` and a lock holder;
    - in P1, BACnet re-discovery has run and the new points are arriving.
12. Run `XINFO GROUPS` twice more. Lag must return to the pre-deploy value
    within 10 min with a negative slope. If it does not, roll back (§6).
13. Rows seeded on are live; L3/L4 rows stay inert until the first fit. Watch for
    30 min and PATCH off any family that breaks a budget.
14. Ask the user to hard-refresh the browser.

---

## 6. Rollout, rollback and observability

**Switches**

| Switch | Effect | Cost |
|---|---|---|
| `PATCH /detectors/{id}` `enabled=false`, or an override | Stops the detector at the next sweep and clears its open derived alerts (`cleared_by='detector-disabled'`) | No restart |
| `PATCH /detectors/L5` `major_enabled` / `use_influence` | MAJOR escalation on or off; zone or room grouping | No restart |
| `DCIM_ANOMALY_ENABLED=false` | Worker steps become no-ops | Ingest restart, which creates an offset; check XINFO |
| Migration downgrade (`29` §13) | Removes seeds or tables | Restart; last resort |

**Per phase**

| Phase | Rollout | Rollback |
|---|---|---|
| 0 | Flag on, no detectors | Flag off; `downgrade 0110`; revert the simulator commit. Note that the C2 data fix stays correct either way |
| 1 | Seeded families go live under the MINOR cap; watch for 30 min | Disable the family, or turn the flag off; `downgrade 0111`. The collector quality change is independent: revert and rebuild it on its own if it misbehaves |
| 2 | `L5` on with `major_enabled=false` | `major_enabled=false` or disable `L5`; clear symptom marks with a one-off call |
| 3 | Two nights of fits first; the daily cap auto-disables noisy detectors | Disable L3; `downgrade 0112` |
| 4 | `use_influence=false` until scored | `use_influence=false`; `downgrade 0114` |
| 5 | Ships with `none` | Set `none` and restart |

**Observability**

- `GET /api/v1/anomalies/health` (`30` + `31` D6) reports:
  - `enabled`, `explainer`, `sweep_s`, `severity_cap`;
  - sweep p50/p95/max, split into read, score and write;
  - series scored;
  - skips by reason: `lock_held`, `cadence`, `ingest_lag`, `disabled`;
  - findings and derived alerts per hour;
  - frames sent, and frames collapsed into a summary;
  - lock holder;
  - fit time, thin buckets refused, rows pruned;
  - errors and auto-disabled detectors.
- Structlog events: `anomaly sweep`, `anomaly sweep skipped`, `anomaly baseline fit`,
  `anomaly prune`, `anomaly detector auto-disabled`.
- Prometheus: `dcim_anomaly_sweep_seconds`, `dcim_anomaly_findings_total{detector,lane}`,
  `dcim_anomaly_sweep_skipped_total{reason}`.
- Self-protection: a sweep that runs past 2× its p95 budget doubles the next
  interval, up to 300 s. The sweep always yields when ingest lag exceeds 60 s.

---

## 7. Budgets (starting values)

| Budget | Value | Gated in |
|---|---|---|
| Sweep p95 | < 0.5 s empty; < 3 s for L0+L2+L5; < 5 s with L3/L4 | 0, 1, 3, 4 |
| Sweep max | < 10 s, otherwise throttle | all |
| Sweep cadence | 60 ± 2 sweeps per hour across both replicas | 0 |
| Nightly fit | < 10 min; < 300 MB transient | 3, 4 |
| RSS delta, lock holder | < 30 / 100 / 150 MB (P0 / P1–2 / P3–4) | 0–4 |
| RSS delta, other worker | < 20 MB | all |
| Redis `dcim:anomaly:*` | ≈ 3 MB (`29` §10) | 1, 3 |
| Host (7.7 GB, WSL VM ~3.8 GB) | No rise in swap-in during sweeps; ingest within 10 % of P0 | all |
| Websocket | ≤ 50 anomaly frames per sweep; 0 slow-consumer disconnects | 1–4 |
| Derived alerts, steady state | ≤ 1/h estate-wide | 1 |
| Derived severity | MINOR until the P2 gate; MAJOR only by L5 root escalation | 1, 2 |
| Upset flood | ≤ 10 non-symptom alerts in the first 10 min (EEMUA 191) | 2 |
| Advisories | ≤ 30/day estate-wide; ≤ 10/day per detector | 3, 4 |
| Anomaly tables | Sized per `29` §6.2, bounded by the nightly prune | 1, 3 |

---

## 8. Risk register

| # | Risk | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
| R1 | **Host RAM/CPU.** The sweep or the fit swaps the WSL VM | High (has happened twice) | High: ingest falls from 180 to 3 ticks/min | Incremental window reads; chunked fit under a session lock; RSS budgets as gates; yield and throttle; no campaigns during deploys; RAM upgrade before P3 (Q4) | Platform backend |
| R2 | **Ingest stall** from a broken worker deploy | Medium | High | Catch-all, as in the staleness sweep; entry-point test; `dev.log` check; flag-off rollback | Platform backend |
| R3 | **Alert fatigue** | Medium | High | MINOR cap; advisories never page; daily cap; chatter collapse; stale flag; budgets as exit gates; operator precision tracked from `verdict` | Platform backend + user (Q7) |
| R4 | **Premature MAJOR → Jira storm** | Medium without the cap | High | Cap until the P2 gate; MAJOR only by L5 escalation; never allow-listed; Q13 | Platform backend + user |
| R5 | **Baseline poisoning** by slow faults or maintenance | High for gradual faults | High | Exclusions (maintenance, ledger, `sim_change`, manual); refit freeze on an open finding; ≤ 10 %/night cap | Platform backend |
| R6 | **No rack↔CRAH mapping** until P4 | Certain until P4 | Medium | Room-level fallback; influence fit scored against `/api/air/influence`; verify the 0099 geometry | Platform backend |
| R7 | **Simulator realism gaps**: no truth for CR-2, CD-5, SV-1/2, UP-1/2, TX-1, GN-1, PL-1, CR-6 before S4; compressed ramps; open-loop CRAH fan; flat file-backed SNMP points | Certain | Medium: detectors "pass" against physics easier than reality | Recommend those detectors off, or synthetic-only; S4 (3.14) when affordable; never claim a gate without ground truth | Simulator + platform |
| R8 | **Forced-alarm couplings overwritten.** CRAH `Alarm_HighTemp` is confirmed overwritten in code; about 7 forced alarms share the pattern (`32` §9.2) | Certain until fixed | High for CR-1: its abrupt truth is fake | S0-9 in P0; S1-10 (CDU) in P1; `32`'s forced-coupling log lists the rest; scenarios on an unfixed coupling are not scored | Simulator |
| R9 | **BACnet quality blind.** The collector stamps every sample GOOD (`bacnet.go:347`) | Certain until 1.21 | Medium: DQ-1/DQ-2 miss BACnet dropout, which then reads as a flatline | Task 1.21 plus its Go tests; `crah-return-dropout` exit criterion | Collector |
| R10 | **Setpoints not honoured** by the simulator today | Certain until S1-8 | Medium: setpoint controls and CUSUM-reset tests would pass vacuously | S1-8 in P1; setpoint scenarios not scored before it | Simulator |
| R11 | **Missing BACnet points** (CRAH filter ΔP, chiller approach; no CRAH valve command; no pump start command) | Certain | Medium | `29` D7–D10 rescopes. New points plus mappings in P1, enabled in P3. On real equipment, point availability depends on vendor and firmware, so the G36 missing-point rule stays mandatory | Platform + simulator |
| R12 | **Statistical, not bit-exact, repeatability** | Certain | Low–Medium: a single run over- or understates | 3× abrupt runs; windows ± one poll; medians and ranges reported; offline twins for gradual faults | Platform harness |
| R13 | **Simulator restart side effects**: protocols drop (alarms 28 → 641); latched condenser trips; fingerprint changes need a regen | Medium | Medium | Deploy steps 6–8; runner clean-baseline check and trip reset; `context` ledger exclusions; DQ-1 gated on ONLINE | Operator |
| R14 | **Accidental live fire scenario** | Low with the guards | High: a real gas-release sequence on the simulated estate | `--allow-fire` plus explicit user approval per run; the runner refuses otherwise | Operator |
| R15 | **LLM hallucination or data egress** (P5) | Medium if enabled | Medium | Off by default; validator with fallback; read-only access; audit log; labelled panel shown below the evidence; Q2 | Platform backend + user |
| R16 | **Double sweep or lock contention** | High without the gate | Medium | Cadence gate; state held in Redis or `anomaly_state`; DCIMB session lock; `_live` tests | Platform backend |
| R17 | **Websocket slow-consumer drops.** `hub.py:46,170-178` closes a session when its 256-frame queue fills, which could drop every console mid-cascade | Medium during cascades | Medium | ≤ 50 frames per sweep plus a summary frame; REST re-sync; fanout-cap test; exit criteria | Platform backend |
| R18 | **Schema or mapping drift** | Medium | Medium | Catalogue test against `metrics_gen.METRICS`; CI `mapcheck` and the bundle check; per-device missing-point disable is reported | Platform backend |
| R19 | **Wrong `detection` on analytics alarms** | Medium | Low–Medium | Numeric `metric_key` or `None`; `test_anomaly_detection_is_derived.py` | Platform backend |
| R20 | **Stale device rollup** | High if missed | Medium | `refresh_device_alarm_state` on every sweep; `_live` rollup test | Platform backend |
| R21 | **Untested authorisation.** `/login` always issues admin (`misc.py:55`) | High if unnoticed | Medium | Hand-minted viewer tokens in every API test file | Platform backend |
| R22 | **Network SNMP timeouts** turn into DQ-1 noise | High if in scope | Low | Network devices excluded; ONLINE gate | Platform backend |
| R23 | **Taxonomy test breaks** | High on first push | Low | Explicit tasks 1.12 and 3.5 edit both files; catalogue test | Platform backend |
| R24 | **Two writers of `is_symptom`** | Medium | Medium | Partition by category; coexistence test | Platform backend |
| R25 | **No frontend test runner** | Certain | Low–Medium | `tsc`, build and live acceptance; Q15 | Frontend |
| R26 | **Contaminated soak** | Medium | Medium: a week lost | Soaks declared in this doc; the scorer refuses any window that contains a non-`context` ledger entry or a deploy | Operator |
| R27 | **Phase 1 is large.** Platform, collector and simulator S1 share one slot | Medium | Medium: a bigger blast radius per deploy | Simulator first with its own verification; collector next; platform last. Each component rolls back independently. Ingest health is checked between steps | Operator |

---

## 9. Open questions for the user

1. **Where derived alerts appear first.** The plan assumes the main alarm list
   from P1, with the new detection select able to hide them.
2. **Explainer.** `none` permanently, `local` (which model, on which host? The
   current host cannot fit a 7–8B model), or `cloud` (egress allowed? which
   provider? what budget?). The plan ships `none`.
3. **Process placement.** Run the detector in the ingest worker (the plan) or as
   a separate process? Decide before P3 if the P1 measurements are marginal.
4. **RAM upgrade.** Should P3 wait for it? The plan recommends yes if swap-in
   rises during the P1 soak.
5. **Fast versus realistic durations.** `32` §13.3 gives both. Gate on fast and
   run realistic as an optional confirmation?
6. **Wall clock.** Three 7-day soaks with no deploys and no simulator changes.
   Acceptable?
7. **Triage owner and budget.** Who triages advisories, and is 30 per day the
   right budget?
8. **PU-3 paging.** Should a pump that fails to prove ever page on its own, or
   only through L5 escalation (the plan)?
9. **Fire.** Confirm there is no detection on the safety path. Approve, or
   decline, each `fire-pre-discharge` run as it comes up. Is an ASD
   baseline-drift advisory wanted at all?
10. **Nightly scoring cron in WSL.** Allowed, or by hand only?
11. **Spatial mode.** Keep it permanently on in the live simulator from P0? P4 is
    impossible without it.
12. **CD-1 margin.** Resolved: `29` now seeds 2.0 K, matching `thermal.py:177` (`29` D18).
13. **Keeping derived alarms out of Jira.** The severity cap (the plan), or an
    `exclude_detections: ["derived"]` policy clause? The clause changes the
    integration config schema and every stored policy.
14. **S4 additive kinds** (~30 h, optional). Which, if any? The plan's order is
    `hunting`, `loop_leak`, `server_thermal`, `bypass`, `fuel_leak`,
    `battery_aging`, `xfmr_cooling`, `server_fan_wear`, then CRAH coil.
15. **Frontend test runner.** Add vitest so `31`'s component tests can run?
16. **Sensor-health screen in P1** (~10 h), or API and settings only until P3?
17. **Seeded-on detectors without ground truth** (CD-5, SV-1, UP-1, TX-1).
    Resolved: `29` now seeds them disabled until simulator S4 (`29` D19).

---

## 10. Contract deviations, collected

| # | Contract or brief says | Code or design says | Recommendation |
|---|---|---|---|
| D1 | "alarms/alert_taxonomy.py" | It is `app/core/alert_taxonomy.py`, plus the test's `NON_TRAP_ALARM_TYPES` | Tasks 1.12 and 3.5 edit both |
| D2 | 0112 seeds "taxonomy rows if table-backed" | The taxonomy is code (`29` D5) | No taxonomy SQL |
| D3 | "Use exactly doc 28's IDs" | L0 has no IDs | DQ-1…DQ-6, with DQ-6 in 0114 (`29` D1) |
| D4 | (`28`: alarm_type "CR-1") | Alarm types are snake_case condition names | Use the `29` §9.3 names |
| D5 | Derived alerts MINOR/WARNING | `28`: PU-3 "MAJOR candidate"; `30`: MAJOR tickets after 300 s | Cap until the P2 gate; MAJOR only by L5 escalation (Q8, Q13) |
| D6 | (`28`: evidence column?) | `alarm.attributes` exists (0005:99) | Optional kwarg; no migration |
| D7 | `28` scopes VA-1, PU-3, CR-4, CH-3, PL-1/2 broadly | BACnet points are missing (`29` D7–D11) | VA-1 valves only; PU-3 proof vs process; CR-4 and CH-3 on new points (1.22, 3.13); PL-1 per chiller, off; PL-2 not seeded |
| D8 | (`28`: rollup "for free") | `raise_alarm` does not refresh `device_state` | Refresh once per sweep (`29` D12) |
| D9 | Lock constants "pick distinct" | The lock alone admits a double sweep | DCIMA plus the cadence gate; DCIMB as a session lock (`29` §10.1) |
| D10 | Degradation kinds: cop, tower_approach, hx_approach, filter_loading, pump_derate | No ground truth for GN-1, CD-5, UP-1/2, TX-1, SV-1/2, CR-2, PL-1, CR-6 | `32` S4 kinds (optional, 3.14); until then synthetic-only, and recommend off where `29` seeds on |
| D11 | Ledger `{id, kind, device, point, start, end, params}` | The scorer needs scenario, kind and context | `32` D5 fields (`campaign`, `expected_kind`, `device_id`, `context`) |
| D12 | No home for the scorer | — | `app/analytics/evaluation.py`, `backend/scripts/anomaly_eval.py`; expectations read from `S:tools/anomaly_campaigns.json` |
| D13 | L5 has no detector row | One rollback mechanism | Catalogue row `L5` (for `29`'s 0112) |
| D14 | `29` seeds CD-1 at 1.0 K | Display is 2.0 K (`thermal.py:177`) | 2.0 K (Q12) |
| D15 | `29` seeds CD-5, SV-1, UP-1, TX-1 on | No simulator ground truth | Off, or PATCH off at P3 (Q17) |
| D16 | Telemetry band "via `Plot.tsx`" | The tab uses `TimeChart`, and `Plot` has a y-range bug (`31` D2) | Extend `TimeChart` (3.9) |
| D17 | "NumberInput" | Absent in `P:frontend` (`31` D1) | Port it (1.17) |
| D18 | (`31` §18) triage tab in P3 | L0 findings exist from P1 | Accept, or pull forward (Q16) |
| D19 | `PYTHONHASHSEED` (`28`) | `32` D9 uses stable hashing plus `DCIM_SIM_SEED` | Adopted |
| D20 | `32` S1 "before platform L3" | P1 already rebuilds the collector; setpoint controls need S1-8 | S1 ships in the P1 slot |
| D21 | `32` library | No CD-1 abrupt scenario, no VA-2 scenario, no RPP-trip root | Resolved: `32` §13.2 now defines `valve-hunting` and `rpp-trip-crah` (rooted at `MPPA-DC1-HA`, needs S1-11); CD-1 is scored in P3 via `cdu-hx-fouling` |
| D22 | BACnet quality (`32` §4.5) | Collector stamps GOOD (`bacnet.go:347`) | Task 1.21 |
| D23 | (`28`: "next migration after 0108") | Head is 0110 | 0111–0115 with the `29` §13 names |
