# 34 — Power page: build plan

Status: **Phases 0–6 built and deployed 2026-10-27 (platform through 5a6c3ce, simulator through e63b484, SNMP regenerated 894/894 each time); Phase 5 proven live by injection the same day. §7's questions are answered.** Written
2026-10-27 from a read of the tree (`P:` means `dcim-platform/`, `S:` means
`Datacenter_Network_Simulator/`; lines checked against platform `f24b438` and
simulator `1c273ac`).

Built-time deviations from the plan below, so the next phase starts from
what exists:

- The windowed queries read raw samples for 5-minute buckets, `telemetry_5m`
  under an hour and `telemetry_1h` beyond (`repositories/estate.power_source`),
  because a week of raw power_draw was three four-second scans. A note on the
  page names the rollup's lag.
- Rack load is the IT and network devices' own draw, not the strip's meter,
  so racks fold into their room's IT column exactly; the note says the strip
  reads a few percent above it.
- The kW/kVA switch divides by a nominal 0.95 power factor and says so;
  nothing on these rows meters apparent power (1.10 as planned, open
  question 4 answered by shipping the cheap version).
- NOW's delta compares the hot mirror with ten minutes of raw samples
  centred fifteen minutes back, not the 5-minute rollup, because the rollup
  trails its refresh.
- Phase 0's coincident site peak is a second query over the same bucket CTE
  (`power_window_peaks`), run only in PEAK mode.
- Phase 2 is ONE endpoint, `GET /estate/power-trend`, feeding three panels
  (`features/estate/PowerTrend.tsx`); React Query dedupes the three reads
  into one request. The planned "demand" panel is folded in: the Draw panel
  marks the window's coincident peak as a labelled reference level and the
  third panel draws IT as a share of the scope's rating as columns coloured
  by band (the alarm trend's form), which is the capacity question the
  demand chart was for. The rating a trend divides by follows the table's
  rule (`services/estate._scope_rating`). Thirty days of daily points read
  the hourly rollup in about 7 s warm on this host, the same as the thermal
  trend; a week of hourly points is under a second warm.
- Phase 3 reuses the plant service rather than a second set of readers:
  `services/plant.supply()` reads the electrical spine wherever it stands
  (new `repositories/plant.supply_machines/supply_latest`, `SUPPLY_TYPES`)
  with the same readers, state labels and verdicts the facility rooms use;
  `services/power.scope_summary` lays the chain's load on top, reads RPPs
  through their EV2 (`metered_by`, stale after 15 min), adds `load_high`
  and treats a transformer's natural cooling as normal
  (`supply_verdict`). The census is `redundancy_census` (IT loads by cords)
  plus `plant_groups` (room × type, judged on members fed: ok / tight /
  short). `GET /power` takes `datacenter_id` and `room_id`. The SUPPLY tab
  is `/power?scope=supply` with a site Seg; the analytics Power view is
  gone. The planned verdict table (3.4) lives in the plant service's
  `_electrical_verdict` plus the two chain findings, not a new table.
- Phase 4 rides on the existing power payload rather than new endpoints,
  except one: `GET /estate/rooms/{id}/panels` (RPPs read through their EV2,
  the `PanelMetering` component). Headroom and stranded kW come from ONE
  query, `repositories/estate.power_p95` (7-day p95 of the coincident hourly
  IT draw from `telemetry_1h`, GROUPING SETS at rack / room / site / estate),
  so no per-row capacity call. Energy PUE is `services/pue.compute` over the
  last 24 h in NOW and over the window otherwise, for the estate and each
  site; the header shows it with its category (`L1`) and keeps the draw
  ratio in the tooltip, falling back to the ratio only when no counter
  answered. Runway is NOT in the payload: the page asks the existing
  forecast endpoint for the drilled scope only (`metric=it_power`,
  `capacity=rated_kw`), because a projection per row is ninety days of
  history per row; on this estate it refuses (insufficient history) and
  the cell says so. The A/B check (`services/estate.rack_shared_upstream`)
  names RPP / UPS / ATS devices common to both sides' closures and ignores
  the switchgear and utility a single-feed site shares by design. No
  migration (4.6).
- Phase 5, platform side: `alarm_rule` `psu-failure` (migration 0115:
  boolean on `psu_state`, raise on False, MAJOR, dwell 2 - the BMC's
  Status.State/Health has been stored per supply since the Redfish adapter
  was written and never evaluated); `repositories/estate.power_rack_legs`
  (latest `current` per leg per strip, NEMA imbalance derived) and
  `power_rack_psu_failed` (latest `psu_state` False per supply) folded onto
  rack rows and counted up the tiers; the Feeds cell says "on one PSU" /
  "dual · 23 %" and carries the legs in its tip; the APC mapping walks
  rPDU2PhaseStatusTable as a table (instance = leg) instead of GETting row 1
  (`contracts/mappings/snmp/standard.yaml`, bundle synced, collector
  rebuilt); `services/power.supply_verdict` adds `runtime_short` for a UPS
  under 5 min against the 15 s generator window the simulator's NFPA 110
  sequence takes (`GENSET_WINDOW_S`, `UPS_RUNTIME_FLOOR_MIN`).
- Phase 5, simulator side (committed 6e78aff, deployed with the SNMP
  regeneration): `set_psu_condition` + `psu_fail` in FAULT_MAP (a
  failed supply joins the dead-cord set, so the survivor carries the whole
  chassis and the strip on that side sees nothing; Redfish reports the
  supply UnavailableOffline/Critical with 0 W); `_pdu_leg_currents` sums
  each outlet's live share onto the leg its bank sits on (230/400 V Wye:
  one leg per bank, V(L-N) x PF) and the strip's imbalance is now NEMA's
  from the legs, the random walk surviving only on strips nothing is
  plugged into; `_pdu_entries` seeds one rPDU2PhaseStatus row per leg and
  one bank row per bank, `patch_metrics` writes them live. The new rows
  change the SNMP dataset fingerprint: deploy = commit, Stop → Regenerate →
  Start SNMP (894/894), sim restart with the usual env. Raritan
  inletPoleSensor rows are NOT added: every Raritan strip here is 1-phase
  and the pole-table OIDs were not verified against PDU2-MIB.
- Phase 5, live proof 2026-10-27 (both injections cleared after):
  `psu_fail` on SRV01-DC1-HA-R2-01 raised `psu_failure` MAJOR on point 0
  after the second Redfish poll (the server polls about every 4.5 min, so
  dwell 2 is roughly 9 min from the fault, and the same to clear); rack
  R2-01 showed the failed supply in Feeds; PDUB-DC1-HA-R2-01 leg 1 carried
  15.4 A against PDUA's 12.2 A, the B strip 8510 W against A's 7640 W.
  `pdu_imbalance` on PDUA-DC1-HA-R2-02 raised `phase_imbalance` MAJOR via
  the trap path. The failed-supply label is one-based (`_psu_label`): the
  Redfish MemberId is a 0-based index, the faceplate says PSU1. The alarm
  said "Power supply 0" until 2026-10-28: the label moved to `core/psu.py`
  and `alarms/engine._render_state` uses it for `psu_state`, so the alarm and
  the Feeds cell name the same supply (`tests/test_psu_alarm_label.py`).
- Phase 6 (2026-10-27, user asked for "items 1 and 2"): **On failover** and
  **Worst bank** on rack rows, **Feed risk** counts on rooms and sites.
  Failover (`services/estate.rack_failover`) judges each side as the survivor
  of the other's loss at three levels and reports the one that binds: the
  whole rack on the survivor's summed rating (assumes every load is
  dual-corded), the two strips' same-numbered legs on the survivor's per-leg
  rating, and their same-numbered banks on its bank breaker (both assume
  mirrored cording; one strip a side only). Banks come from two new keys,
  `bank_current` and `bank_breaker_rating` (migration 0116), walked from
  rPDU2BankStatusTable and rPDU2BankPropertiesTable (.26.8.2.1.5, verified
  against PowerNet-MIB); amber from 80 % of the breaker. The simulator side
  (bank currents summed from the outlets, breaker rule: 16 A banks on IEC
  strips over 16 A, 20 A on 208 V strips over 20 A) shipped in simulator
  b4b1dfd: 56 APC strips publish bank breakers. First live read: 11 racks
  would overload the survivor, all on a leg - the bank-order cord fill below.
- Cord spread (simulator e63b484, platform 5a6c3ce, 2026-10-27): the
  simulator's `next_free_outlet` filled banks in order (18 racks over 10 %
  leg imbalance at ~64 % load). Cords are now spread heaviest-first across
  legs then banks, as installers do, and the platform re-seated its cords
  with `python -m app.importer.resync_outlets --apply` (752 outlet rows, 780
  cords; never the full importer). Live after: worst bank 109 % -> 51 %,
  racks with a bank >= 80 % 12 -> 0, racks >= 100 % on failover 11 -> 3
  (worst 218 % -> 101 %, bank-bound on dense R2 compute racks - a real
  finding, kept), racks over 10 % leg imbalance 18 -> 5 (worst 16 %, live
  draw variance).
- Raritan branch breakers (2026-10-28, simulator adf55ab, platform
  d2426d7): the 24 PX3-5503U strips publish their two 16 A overcurrent
  protectors (C1, C2) in PDU2-MIB - measurementsOverCurrentProtectorSensor-
  Value [pdu][ocp][1] and overCurrentProtectorRatedCurrent [pdu][ocp], a
  DisplayString ("16A") read by the collector's opt-in `number_in_text`
  transform - into the same `bank_current` / `bank_breaker_rating` keys as
  APC, so Worst bank and On failover cover their racks. OIDs verified
  against PDU2-MIB on mibs.observium.org; the exact rating string a real
  unit returns was not confirmed by a walk.
- Branch breakers trip (2026-10-28, UNCOMMITTED at writing). Simulator:
  per-bank breaker state (`set_pdu_bank_trip`, latching, reset by hand) -
  only the outlets on that bank go dead, so a dual-corded load rides it on
  the other strip, which takes the whole draw and can trip in turn. Trips
  also come from sustained overload: a thermal element after IEC 60898-1
  (C curve) - never at 1.13 In, ~6 min at 1.45 In, ~60 s at 2.55 In - so a
  bank at 100 % of its rating carries it indefinitely (today's worst
  failover, 101 %, does not trip). Fault `pdu_bank_trip` (instance = bank).
  The BMC reports a supply whose cord is dead as lost input (Enabled /
  Critical, 0 V), as iDRAC and iLO do, so Feeds shows "on one PSU" for the
  servers a bank trip leaves on one cord. Traps: Raritan
  overCurrentProtectorSensorStateChange on the protector table with its
  label (instance "C1"); APC 318.0.226 naming the bank; resets on Raritan's
  OCP trap or, for APC (PowerNet has no breaker object), the plane's
  99999.6.21 with a 99999.5.21 bank varbind. Platform: `traps.yaml`
  regenerated (generator keys entries by matcher too, so the strip-wide and
  per-breaker Raritan trips coexist on one OID); polled backstop
  `bank_breaker_closed` (Raritan trip sensor, instance "C1") with rule
  `bank-breaker-tripped` (migration 0117) on the same alarm key as the trap.
  APC has only the trap and its reset.

The brief: bring `/power` up to the standard of `/thermal`, in features and in
UX, using the data the platform already holds before asking the simulator for
more. Thermal is the reference for every UX device named here; where this plan
says "as Thermal does", the file and line it means is in §2.

Every phase ships on its own and lands as one deploy, because the WSL host is
tight (see `feedback_batch_deploys_tight_host`). Frontend work ships on commit
(standing rule); the migration-free backend work ships with it.

---

## Summary

| Phase | Ships | What the operator sees | Effort |
|---|---|---|---|
| 0 ✓ Fix what is wrong | Same-day window 400, non-coincident site peak, Δ blank in NOW, PUE threshold disagreement | The numbers the page already shows stop lying | ~6 h |
| 1 ✓ Thermal parity | Rack level, breadcrumb, header help + cell tooltips, severity lead, load-vs-rating columns and spread bar, alarms column + slide-over, coverage KPI, kW/kVA switch, NOW / LAST HOUR / BY DAY windows, freshness note | A page that reads like Thermal and drills to the rack | ~40 h |
| 2 ✓ Trends | Three panels on the shared range control: draw stacked (IT / cooling / other), PUE line, demand with the coincident peak marked; maximize, crosshair, scope-following | Where the load has been going | ~22 h |
| 3 ✓ Supply chain tab | PLANT-style tab: utility → genset → switchgear → ATS → UPS → MCC/MPP → RPP rows with state, load %, measured/derived, verdict; redundancy census reframed; scope honoured; RPP read through its EV2 | Is the feed healthy, and what is on one leg | ~36 h |
| 4 ✓ Headroom and panels | Energy-based PUE headline with method badge, headroom and runway per room, stranded kW per rack, A/B same-upstream check, EV2 panel circuits per room | Capacity questions answered on the page | ~34 h |
| 5 ✓ Simulator-gated | Per-leg PDU currents, live per-outlet current, PSU failure, UPS runtime vs genset window | Only after `S:` grows the model | later |

**Total through Phase 4: ~138 h.** Phase 5 is not estimated; it depends on
simulator decisions in §7.

---

## 1. Decisions that shape the build

1. **Load against rating is the headline per row, not PUE.** A floor manager
   reads "kW of rated" on a rack and a room; PUE per room is a derived ratio
   that two rooms with opposite problems can average into a healthy number.
   PUE stays in the header and the site rows. Row lead colour comes from the
   worst of: load band, open power alarm severity, redundancy verdict.
2. **Bands: 80 % amber, 90 % red**, the EkkoSense-style bands `docs/27` §7.2
   already names for the twin's power layer. One source for both pages:
   a `POWER_BANDS` constant in `P:backend/app/core/alert_taxonomy.py`
   returned by the estate endpoint, never typed into the frontend.
3. **Energy-based PUE is the truth; the power ratio is the live hint.** The
   page today shows `total_kw / it_kw` and calls it PUE; `/analytics/pue`
   computes the Green Grid kWh figure with a category badge. Phase 4 puts the
   energy figure in the header with its badge and demotes the ratio to a
   tooltip ("instantaneous ratio 1.27"). Until then the header says
   "PUE (power ratio)" so the two pages stop disagreeing silently.
4. **The redundancy census splits IT loads from plant loads.** Today 50 of the
   54 "loads without full redundancy" are chillers, pumps, CRAHs and towers.
   Mechanical loads are single-corded by design; their redundancy is N+1 units
   behind an ATS, not two cords. The census becomes two numbers: dual-cord IT
   loads on one path (the maintenance question), and plant N+1 groups with a
   member down (the cooling question). The verdict code in
   `P:backend/app/services/power.py:99` stays; the grouping is new.
5. **No new hypertable, no new migration before Phase 4.** Phases 0–3 read
   `telemetry_sample`, `device_state`, `alarm`, `device_endpoint` and the
   topology edges that exist. Phase 4 adds one migration (`0115`) only if the
   headroom query needs a materialised room design-kW column; `docs/20` already
   carries `design_it_kw` on the room, so probably not.
6. **Every number says where it came from.** Measured vs derived (the chain
   service already labels it), reporting X of Y, as-of time. This is what
   Thermal's Readings column and freshness line do, and the reason it is
   trusted.

---

## 2. Verified code seams

Frontend (`P:frontend/src/`):

| Seam | Where | Used for |
|---|---|---|
| Power page | `features/estate/Power.tsx` (217 lines) | rewritten in Phase 1 |
| Thermal page | `features/estate/Thermal.tsx:649-680` breadcrumb, `:697-751` KPI band, `:521-558` alarms column, `:857` room-details button, `:892` rack row link | copy the devices, not the file |
| Shared range + panel | `features/estate/ThermalTrend.tsx:261` `useTrendRange`, `TrendPanel` below it, `components/MaxModal.tsx:25` | Phase 2 panels |
| Table scaffolding | `components/estate.tsx` (PageHead, ScopeTabs, DataTable, Num, Delta, Notes), `features/estate/useEstateTable.ts` | keep; extend for a rack level the way Thermal drills |
| Alarm slide-over | `features/home/AlarmPanel.tsx:444` `AlarmPanel({categories, title, scope})` | Phase 1 alarms column |
| Room drawer | `features/home/RoomDrawer` | Phase 1 room details button |
| Rack view power overlay | `features/racks/RackElevation.tsx:42-55` (`overlay=power`) | Phase 1 rack row link target |
| API client | `api/client.ts:4989` `estatePower`, `:5008` `estateAlarms`, `:5019` `thermalTrend` (shape to mirror) | all phases |
| Theme tokens | `index.css:10-40`, `:223` light; `--cat-pwr` amber | all phases |

Backend (`P:backend/app/`):

| Seam | Where | Used for |
|---|---|---|
| Estate power route | `api/v1/estate.py:71` | Phase 0/1 params |
| Estate power service | `services/estate.py:1036` `power`, `:1073` `_power_row`, `:1110` `_fold_power_sites` | Phase 0 peak fix, Phase 1 rack rows |
| Window query | `repositories/estate.py:1371` `power_window` (per-room per-bucket CTE), `:1452` `power_live` | Phase 0 site peak, Phase 2 trend |
| Rack allocated/reserved | `repositories/racks.py:232` `room_rack_power_plan`, `:351` `site_room_capacity` | Phase 1 rack columns |
| Rack rated | `rack.rated_power_kw` (docs/27 §8a) | Phase 1 bands |
| Fleet power + census | `services/power.py:287` `scope_summary`, `:236` `chain_for`; `repositories/power.py:90` `phase_imbalance` | Phase 3 |
| Panel metering (EV2) | `services/panel_metering.py:100`, `api/v1/devices.py:195` | Phase 3 RPP status, Phase 4 circuits |
| Energy PUE | `services/pue.py:78` `compute`, `api/v1/analytics.py:23,46` | Phase 4 header |
| Forecast | `api/v1/analytics.py:125` (`metric=power`) | Phase 4 runway |
| Capacity | `api/v1/capacity.py:18` | Phase 4 headroom |
| Power alarm types | `core/alert_taxonomy.py:479-558`; `power` category | Phase 1 alarms column |
| Tests to extend | `tests/test_estate.py`, `tests/test_power.py`, `tests/test_pue.py`, `tests/test_capacity.py` | every phase |

Simulator facts that bound the plan (`S:`):

- Rack PDU exposes one per-phase current and an imbalance %, not L1/L2/L3
  (`S:core/snmprec_generator.py:1157-1278`). Per-outlet current is the value
  from dataset generation, zeroed when off; it does not follow load.
- RPP has no protocol; it is read through the Verdigris EV2 clamped on it
  (`S:core/bacnet_ev2_generator.py`, platform `/devices/{id}/metering`).
- UPS runtime, on-battery seconds and the NFPA 110 transfer sequence are
  modelled (`S:core/power_transfer.py:52-78`, `S:core/device_state_store.py:9296`).
- Server PSU health is always OK (`S:core/redfish_data_generator.py:418`).
- STS and busway are not modelled.

---

## 3. Phases

### Phase 0 — Fix what is wrong (~6 h)

| # | Task | Files | Test |
|---|---|---|---|
| 0.1 | Same-day window: `end` becomes the start of the day AFTER "To" (`to + 1 day` at 00:00 UTC). Keep the inclusive comment true. | `Power.tsx:46` | vitest: `from === to` yields a 24 h window |
| 0.2 | Coincident peak at site and estate level. Return `buckets` per room from `power_window` is not enough: add a second query (or a second CTE output) that sums rooms per bucket per site before `max()`. Fold sites from that, not from room peaks. | `repositories/estate.py:1371`, `services/estate.py:1110` | `test_estate.py`: two rooms peaking in different buckets give a site peak below their sum |
| 0.3 | Δ in NOW mode: compare the hot mirror against the 15-minutes-ago 5-minute rollup (`telemetry_5m`), the way Thermal's NOW rate works. | `repositories/estate.py:1452`, `services/estate.py:1073` | `test_estate.py` |
| 0.4 | One PUE threshold. Header 1.6, rows 1.5/1.8 today. Until Phase 1 replaces the row lead, both read `POWER_BANDS.pue_warn` from the response. | `Power.tsx:126,189`, `alert_taxonomy.py` | snapshot of thresholds in the response |

Exit: the four numbers above change on the live page; nothing else does.

### Phase 1 — Thermal parity (~40 h)

Backend (~14 h):

| # | Task | Files |
|---|---|---|
| 1.1 | `GET /estate/power` grows `racks[]` when `?room=` is given: per rack `metered_kw` (sum of `device_state.power_w` for `it`/`network` devices in the rack, PDUs excluded as today), `allocated_kw`, `reserved_kw`, `rated_kw`, `load_pct` (metered / rated), `band`, `feeds` {a: pdu name, b: pdu name, dual_cord_devices, single_cord_devices} from the power edges, `reporting` (devices with a reading / devices). | `repositories/estate.py` (new `power_racks`), `repositories/racks.py:232` reused, `services/estate.py:1036` |
| 1.2 | Room and site rows grow `rated_kw` (sum of rack ratings, or `design_it_kw` when the room has it, labelled `rating_basis`), `load_pct`, `band`, `spread` {under60, b60_80, b80_90, over90: rack counts}, `reporting` {racks, devices}. | same |
| 1.3 | `alarms` per row: open alarms in the `power` category, split `rack` (device in a rack) vs `plant` (floor-standing), worst severity. Mirror `thermal_alarms` at `repositories/estate.py:1300`. | `repositories/estate.py`, `services/estate.py` |
| 1.4 | `window.mode` gains `hour` (last 60 min, 5-min buckets) alongside `live` and the date window, so the page has NOW / LAST HOUR / BY DAY like Thermal. | `api/v1/estate.py:71`, `services/estate.py` |
| 1.5 | Response carries `bands` and `as_of` (newest `device_state.updated_at` in scope). | `services/estate.py` |

Frontend (~26 h):

| # | Task | Files |
|---|---|---|
| 1.6 | Rewrite `Power.tsx` on Thermal's skeleton: PageHead with six KPIs (Total, IT, Cooling, PUE (power ratio), Load of rating, Rooms reporting), breadcrumb All sites › site › room with back, selected-scope strip with Room details button opening `RoomDrawer`. | `Power.tsx`, `Thermal.tsx:649-751` as reference |
| 1.7 | Three drill levels. Rack row click → `/racks/{id}?overlay=power&from=power`. | `useEstateTable.ts` (rack level), `Power.tsx` |
| 1.8 | Columns: Name · Load kW · of rating (% with band colour) · Spread bar (four segments, `stack-bar` classes) · IT · Cooling · Other · PUE · Alarms (count, opens `AlarmPanel({categories:['power'], scope})`) · Feeds (rack rows: A/B names, single-cord count in amber) · Readings. Every column has `help:`; every cell a tooltip saying what it is or why it is missing. | `Power.tsx` |
| 1.9 | Row lead from `severity()` = worst of band, alarm severity, feed verdict. | `Power.tsx` |
| 1.10 | kW / kVA segmented switch (kVA from `apparent_power` where metered, else kW ÷ 0.95 with a "nominal PF" tooltip). Persist in `localStorage` like °C/°F. | `Power.tsx`, `components/estate.tsx` |
| 1.11 | Window control NOW / LAST HOUR / BY DAY with focus-day and compare-day pickers; refetch 30 s / 60 s / none. | `Power.tsx` |
| 1.12 | Notes: as-of line; rating basis per room ("design 300 kW" vs "sum of 56 rack ratings"); the facility-rooms reconciliation stays. | `Power.tsx` |
| 1.13 | CSV gains the new columns. | `Power.tsx` |

Tests: `test_estate.py` for 1.1–1.5 (rack rows, spread counts, alarm split,
hour window); vitest for band colouring and the kVA switch; a playwright shot
of site, room and rack level in dark and light, 1440 and 420 px.

Exit: a rack with a single-cord server shows amber in Feeds; a rack over 90 %
of rating shows red lead; clicking Alarms opens the slide-over scoped to the
room; the page passes the UX checklist in §5.

### Phase 2 — Trends (~22 h)

| # | Task | Files |
|---|---|---|
| 2.1 | `GET /estate/power-trend?site|room|rack&since&until&bucket=hour|day` → per bucket `it_kw`, `cooling_kw`, `other_kw`, `total_kw`, `pue`, `devices`. Hourly from `telemetry_1h` where the scope is a site, from `telemetry_sample` bucketed for a rack. Same shape and params as `thermal-trend` at `api/v1/estate.py:245`. | `api/v1/estate.py`, `repositories/estate.py` (new `power_trend`), `services/estate.py` |
| 2.2 | `GET /estate/power-demand-trend`: per bucket coincident `total_kw` plus the window's single peak bucket, so the chart can mark it. | same |
| 2.3 | `PowerTrend.tsx`: three `TrendPanel`s on one `useTrendRange`: Draw (stacked area IT / cooling / other), PUE (line, with the energy PUE as a dashed reference once Phase 4 lands), Demand (line, peak marked, rating drawn as a reference line when the scope has one). Crosshair readout, caption with scope / window / device count, maximize, previous line held while loading. | `features/estate/PowerTrend.tsx`, reuse `ThermalTrend.tsx` internals by extraction into `components/trend.tsx` if the duplication exceeds the chart body |
| 2.4 | Panels follow the drill level; hourly refetch every 5 min. | `Power.tsx` |

Tests: `test_estate.py` for bucket maths and the peak marker; vitest for the
range key; shots at 7D and 90D.

Exit: the Demand panel's marked peak equals the table's PEAK figure for the
same window, by construction (same query).

### Phase 3 — Supply chain tab (~36 h)

| # | Task | Files |
|---|---|---|
| 3.1 | `scope_summary` honours `datacenter_id` and `room_id` (the frontend already sends the first; the backend drops it). | `services/power.py:287`, `api/v1/power.py:19` |
| 3.2 | RPP status through its EV2: when a supply hop is passive (`metrics_enabled` false) and has a `monitored_panel` meter, take status / load from the metering service, label `load_source: "metered_by"` with the meter name. Today the row reads UNKNOWN. | `services/power.py`, `services/panel_metering.py:100` |
| 3.3 | Census split (§1.4): `it_loads` {two_n, single_feed, no_feed} and `plant_groups` [{group: "DC1 chillers", n, required, healthy, verdict}]. Plant grouping by room + device_type for chiller / pump / tower / CRAH / CDU; `required` from the cooling plant's N+1 rule already in `services/plant` (the Thermal PLANT tab's "Redundancy" KPI). | `services/power.py`, `services/estate.py` |
| 3.4 | Verdict sentence per supply row, like `RoomCooling`'s Verdict: "on bypass", "load 92 % of rating", "fed from side A only", "genset running 14 min, fuel 61 %". Rule table in one place. | `services/power.py` (new `verdict_for_supply`) |
| 3.5 | `SUPPLY` tab on `/power` (`?scope=supply`): the six KPIs become Utility, On generator, UPS load, UPS runtime (min at current load), On one leg, Phase imbalance (worst). Table: Supply · Type · Site/room · Status · Load % · kW · Source (measured / derived / metered by) · Alarms · Verdict. Census strip above. Phase-imbalance table folds into a column. | `features/estate/PowerSupply.tsx`, `Power.tsx` |
| 3.6 | The analytics Power view becomes a link to this tab; its code goes. | `features/analytics/PowerView.tsx`, `App.tsx` |

Tests: `test_power.py` for scope filtering, the metered-by hop, the census
split and each verdict rule; a shot of the tab.

Exit: RPPs show a status and a load; the single-fed count on a clean estate is
0 IT loads and 0 plant groups short; filtering to DC2 changes every number.

### Phase 4 — Headroom and panels (~34 h)

| # | Task | Files |
|---|---|---|
| 4.1 | Energy PUE in the header: `compute` for the focus window (24 h in NOW), with category badge and plausibility; power ratio moves to the tooltip. | `services/pue.py:78`, `services/estate.py`, `Power.tsx` |
| 4.2 | Headroom per room and site: `rated_kw − p95_kw` and the runway in days from the forecast service, with its confidence band and method. Column "Headroom" with tooltip "p95 over 7 days against design 300 kW; 4.1 kW/week trend → 61 weeks". | `api/v1/capacity.py:18`, `api/v1/analytics.py:125`, `services/estate.py`, `Power.tsx` |
| 4.3 | Stranded kW per rack: `allocated_kw − metered_p95_kw`, summed per room; a "Stranded" KPI on the room strip with the top five racks in its tooltip. | `repositories/racks.py:232`, `services/estate.py`, `Power.tsx` |
| 4.4 | A/B same-upstream check on rack rows: run `chain_for` for the rack's two PDUs and flag when `shared_upstream` is non-empty (two strips on one RPP or one UPS). Cache per rack for an hour. | `services/power.py:236`, `services/estate.py` |
| 4.5 | Panel circuits panel at room level: every EV2 in the room, per branch kW / A / PF, spare circuits, phase A/B/C currents with imbalance %. Reuse the DeviceDetail metering table. | `features/estate/RoomPanels.tsx`, `api/v1/devices.py:195` |
| 4.6 | Migration `0115` only if `design_it_kw` must be backfilled from the simulator export for rooms that lack it; otherwise none. | `alembic/versions/`, importer |

Tests: `test_pue.py` (header path), `test_capacity.py` (headroom shape),
`test_power.py` (shared-upstream flag), `test_estate.py` (stranded sum).

Exit: the header PUE carries a method badge; a room's headroom and runway
appear with their basis; a rack whose two strips hang off one RPP shows the
flag.

### Phase 5 — Simulator-gated (not estimated)

Each needs `S:` work first and a dataset regeneration (fingerprint change,
Stop → Regenerate → Start, SNMP 894/894):

- Per-leg L1/L2/L3 currents on 3-phase strips (`rPDU2PhaseStatusTable` on
  APC, `inletPoleSensor` on Raritan), per-bank breakers, live per-outlet
  current following the outlet's load. Then the rack row shows leg balance.
- Server PSU failure (Redfish `PowerSupplies[].Status.Health`), so the Feeds
  column can show "running on one PSU".
- UPS runtime against the genset start window: the sim has both; the platform
  needs `battery_runtime` and the ATS transfer timing on the same row.

---

## 4. Deploy order and procedure

One phase per deploy. For each:

1. Backend tests on Windows (`.venv\Scripts\python -m pytest tests/test_estate.py tests/test_power.py -q`), `ruff check app tests`, frontend `npx tsc --noEmit`.
2. Commit, push, CI green (REST API poll; no `gh`).
3. WSL: `git pull --ff-only`; `alembic upgrade head` only when a phase carries a migration; restart the API by its argv (`api_deploy.sh` pattern); vite serves the pulled tree.
4. Check `XINFO GROUPS telemetry.v1` lag before and after; the estate queries run on the hypertable and a bad one shows up as lag.
5. Hard-refresh; shots of site / room / rack, dark and light.

Rollback is `git revert` + the same steps; no phase before 4 changes schema.

---

## 5. UX checklist (from Thermal, applied to every Power table and panel)

- Subtitle states the limit the page judges against ("Load against rating, 80 % amber, 90 % red. PUE is the energy ratio over the window →").
- KPI band of six, each with a tooltip explaining absence.
- Breadcrumb with back; selected-scope strip; details button.
- Header help on every column; tooltip on every cell.
- Row lead colour from worst severity, never from a ratio alone.
- Spread bar with four segments on room and site rows.
- Unit switch (kW/kVA) beside the window control, persisted.
- Window control with per-window refresh rate; focus vs compare on BY DAY.
- Readings / coverage column; measured vs derived labelled; as-of line in Notes.
- Trend panels on one shared range, maximize, crosshair, caption, previous line held while loading.
- Tokens only (`--ok --warn --major --critical --unknown --cat-pwr`); light theme checked; 420 px width without horizontal scroll.

---

## 6. Risks

| Risk | Where | Mitigation |
|---|---|---|
| Rack-level query over `telemetry_sample` on 1,458 endpoints every 30 s in NOW | 1.1 | NOW reads `device_state` only; windows read rollups; rack rows load only when a room is selected |
| Site coincident peak needs a second aggregation pass | 0.2 | Emit per-bucket site sums from the same CTE; one query |
| Chain lookups for every rack (4.4) are expensive | 4.4 | Hourly cache keyed by rack; compute lazily on room drill |
| Plant N+1 "required" has no single source | 3.3 | Reuse the Thermal PLANT redundancy rule; if it differs per site, read it from the room's attributes with a default of N+1 |
| Energy PUE needs kWh counters that reset | 4.1 | `services/pue.py` already handles resets and reports plausibility; show the badge, never hide a "low confidence" |
| Two pages disagree during the rollout (analytics vs power) | 3.6 | Phase 3 removes the analytics view in the same deploy |

---

## 7. Decisions taken (were open questions)

Answered by building; each line names where the answer lives, checked
against the tree on 2026-10-28.

1. **Bands: 80 % amber, 90 % red, on every tier.** Racks are banded against
   their own rating, rooms and sites against theirs - the plan's §1.2, one
   source for page and twin: `POWER_BANDS` in `core/alert_taxonomy.py`
   (`warn_pct` 80, `critical_pct` 90, `pue_warn` 1.6), returned by the
   endpoint, never typed into the frontend.
2. **Rating basis: the room's (or site's) design IT kW where the inventory has
   one, else the sum of its rack ratings; a rack is its own rating.** Every
   row says which with `rating_basis` (`rack` / `design` / `racks` /
   `rooms`), and trends divide by the same rule
   (`services/estate._scope_rating`).
3. **Census: split.** IT loads are counted by cords (`redundancy_census`);
   plant loads leave that count and are judged as N+1 groups, room x type,
   on members fed - ok / tight / short (`plant_groups`, Phase 3).
4. **kVA: shipped the cheap version.** The switch divides kW by a nominal
   0.95 power factor and says so in its tooltip; no rack row meters apparent
   power. Revisit only if strips that report VA are modelled.
5. **Phase 5: both models, in one regeneration.** Per-leg PDU currents and
   PSU failure shipped together (simulator 6e78aff), then banks (b4b1dfd)
   and the cord spread (e63b484).
6. **Analytics Power view: removed in Phase 3.** The SUPPLY tab
   (`/power?scope=supply`) replaced it; `features/analytics/PowerView.tsx`
   is gone.
