# 31 — Anomaly detection: frontend specification

Status: **specification. Not built.** Reconciled on 2026-10-21 with docs 29,
30 and 33 (doc 30 §13 type corrections and §15 decisions applied). This doc
implements the frontend half of `28-anomaly-detection-design.md`. It shares
names with `29` (data model), `30` (API), `32` (simulator) and `33`
(implementation plan). Where this doc and `30` disagree on a field, `30`
wins. Every place where the shared contract does not fit the code as it
stands is marked **Contract deviation**. §3 collects them and records how
each was resolved.

**Delivery phases** (doc 33 tasks; §18 has the dependencies):

| Phase | Surfaces |
|---|---|
| 1 | Alarm evidence panel, the `?detection=` select (1.16), `NumberInput` and settings (1.17) |
| 2 | Incident tree in the alarm evidence panel (2.7) |
| 3 | Device band and "Under watch" panels (3.10), the triage tab (3.11) |
| 4 | Zone tier and the sensor-health count (4.8) |
| 5 | AI section (5.5) |

Five surfaces change: a triage tab under Analytics, a detail pane for a finding
or an incident, an expected-range overlay on the device Telemetry tab, an
evidence panel on derived alarms, and a settings page for the detectors. No new
top-level page and no new nav entry are added, as doc 28 decided
(`28` "The code already has the slots").

Code references are `frontend/src/…` unless stated otherwise. They were read
on 2026-10-21, with the alembic head at `0110`.

---

## 1. Scope boundary: read this before writing any component

### Pages that change

| Route / mount | File | Change |
|---|---|---|
| `/analytics?view=anomaly` | `features/analytics/Analytics.tsx` | New `anomaly` tab. The room toolbar is hidden on this tab (§5.1) |
| `/analytics?view=anomaly&finding=…` / `&incident=…` | new `features/analytics/AnomalyView.tsx`, `AnomalyDetail.tsx` | Triage list and detail pane |
| `/devices/:id?tab=telemetry` | `features/devices/DeviceHistory.tsx` | Expected-range band, "Under watch" panels, a findings line |
| `/alarms/:id` and the home alarm modal | `features/alarms/AlarmDetail.tsx` (`AlarmDetailBody`) | Evidence panel, **only** when `detection = 'derived'` and `source = 'analytics'` |
| `/alarms` | `features/alarms/AlarmList.tsx` | One `<select>` for detection, held in the URL as `?detection=` |
| `/settings/anomaly-detectors` | new `features/settings/AnomalyDetectors.tsx`; `SettingsLayout.tsx` nav entry; `App.tsx` route | Detectors, overrides, exclusions |
| shared | `components/TimeChart.tsx` | Optional `band`, `marks`, `width` props. The output is byte-identical when they are absent |
| shared | new `components/NumberInput.tsx` | Ported from the simulator web UI (Contract deviation D1) |
| `/thermal` | `features/estate/Thermal.tsx:689-691` | *Optional.* One sub-link, "Anomalies and early warnings →", beside the two that already exist. This is the only entry point outside Analytics |

### Pages that do not change

**`/` Home, including the "Cooling & Environment" strip.** Derived alerts
already land in its counters. Each counter sums `by_category`, which counts
"Every open condition in each domain", alarms and alerts both
(`api/client.ts:2683-2699`, `features/home/Home.tsx:349-357`). The "needing a
response" line under each counter reads `by_category_alarms`, so MINOR/WARNING
derived alerts raise the big number without raising the response line. That is
the doc 28 policy, and it needs no edit. The home alarm panel already has a
detection facet (`features/home/AlarmPanel.tsx:191-193, 587-592`), so
"derived" can already be filtered there.

Also unchanged: `/power`, `/capacity`, `/connectivity`, `/floorplan`, `/world`,
`/racks/*`, `/assets/*`, `/platform`, the device Overview / Alarms /
Connections / Monitoring / Operations tabs, the device header and severity
rail, every other settings page, and the other six Analytics views.

### The rules that keep the boundary

1. **Shared components may only gain optional props, and leaving a prop out
   must render byte-identically.** `TimeChart` is mounted on every device page,
   so §17 includes a snapshot check that a chart without `band` renders the same
   SVG as before. `Plot.tsx` is **not** edited (§7.1).
2. **CSS is additive and namespaced `.anom-`.** It lives in a new
   `features/analytics/anomaly.css`, imported by `AnomalyView.tsx`,
   `AnomalyDetectors.tsx` and the alarm evidence panel. No existing selector
   is edited and no `:root` token is redefined. New tokens may be *added* to
   `:root` in both palettes (§14).
3. **API additions are additive.** No existing endpoint changes shape. The one
   alarm-list change uses a query parameter the backend already accepts
   (`backend/app/api/v1/alarms.py:39-42`).
4. **Web UI only.** Nothing in the simulator's desktop UI changes. The only
   contact with the simulator `webui/` is that `NumberInput.tsx` is *copied*
   from it; the original is not edited.

### Standing UI rules this spec obeys (restated so a reviewer can check them)

| Rule | Source | How it applies |
|---|---|---|
| Load the `ui-ux-pro-max` and `frontend-design` skills before writing or reshaping any component | user rule 2026-10-08 | Required before §5–§9 are built |
| Colour comes only from `:root` tokens; no hex at call sites. SVG presentation attributes do not resolve `var()`, so use `style={{ fill }}` or `useChartColors()`. Use `color-mix`, never hex-alpha concatenation | theme-token rule; `components/seriesColors.ts:50-64` | All new colour goes through tokens, and charts use `useChartColors()` |
| Charts follow the Assets forms: one unit per chart and never a second y-axis; `--series-1..4` in fixed order, dashed from the 3rd; ranges printed; hover snapped to real samples; `role="img"` with a sentence `aria-label`; empty state is a sentence that says why; `.asset-skeleton` while loading; `.asset-panel` chrome with the maximize glyph and `MaxModal` | chart rulebook (repo `CLAUDE.md` "Chart rulebook") | §6.3, §7, §8 |
| Facet filters are a `.seg.facet-seg` strip: 28px, ALL first, the count inside each cell in faint ink, OR within a strip and AND across, an 8px hue dot. Never pill chips | rulebook; `estate.css:439-458`; `settings/components/PolicyEditor.tsx:254-266` | §5.2 |
| Every table that can exceed a screen pages with `components/Pagination.tsx`, or with `usePaged` for a list already in hand | table rule; `components/Pagination.tsx:29-41, 47-60` | Triage, overrides, exclusions, incident members, detector table |
| Numeric inputs use `NumberInput`: the field can be empty mid-edit and is coerced only on blur | NumberInput rule | Detector and override params (§9) |
| The header KPI band stays on the title row, and the title shrinks first. A tile is a number read every visit; a number you sort on is a column | estate-head rule; `components/estate.tsx:24-63` | §5.2 |
| Compact zoom: never a bare `Nvh`. Write `calc(Nvh / var(--dz))`, and convert any pointer or rect value written to CSS with `toCss()` | zoom rule; `index.css:1331-1336`; `lib/zoom.ts` | Detail pane height, tooltips, measured chart widths |

> **Note on zoom.** The standing memory says the root zoom is 0.85, but
> `index.css:1334-1335` is now `--dz: 0.9; zoom: 0.9`. It applies to windows
> 1100–1699 px wide, or 949 px tall or less. A 1280-px window therefore lays
> out at about 1422 CSS px. §13 uses the real value.

---

## 2. How this works in a real datacenter, and what the UI must not pretend

**Common practice.** Fixed thresholds and device fault bits in the unit
controller and the BMS are the hard alarm. Above them sits a separate early
warning stream, fault detection and diagnostics (FDD), where *people triage*
and nothing pages anyone. Examples are Honeywell Forge's rule library,
SkySpark "sparks", EkkoSense's drift alerts and Schneider EcoStruxure's
incident bundling (doc 28 §"Real datacenters"). Under ISA-18.2 and EEMUA 191, a
condition that needs a work order rather than an operator response within
minutes is not an alarm. **The triage view is therefore a work queue, not a
console.** It has no sound, no flashing, no red for advisories, and no effect
on the alarm KPI.

**Vendor-specific.** JCI OpenBlue puts a monetary or kW impact on each fault
and sorts by it. That is the right prioritisation, and we copy it as
`impact_kw`, labelled as an estimate. EkkoSense's "zones of influence" map is
a Phase 4 concern, and its only UI here is an incident's zone tier.

**Rare.** An LLM narrating incidents. Where products do it (Siemens, JCI), it
sits on top of deterministic analytics and uses pre-built prompts. The UI
labels it, collapses it, puts it below the evidence, and hides it entirely
when the explainer is off.

**What the UI must not imply:**

- **An advisory is not a diagnosis.** An L3 residual says "this differs from
  its baseline"; only L2 rules name a fault (doc 28's L2/L3 split). Copy for
  L3 and L4 findings says *"differs from expected"*, never *"failed"*.
- **A `sensor` finding is a maintenance item, not a process event.** It has its
  own view and its own count, so that sensor health is a visible number rather
  than hidden noise (doc 28 "Sensor faults must be ruled out").
- **The expected range is a model output.** It is drawn as a band with its
  basis printed ("fitted on 21 days to 14 Oct, ±3σ"). Without that basis, a
  band reads as a specification limit, which it is not.
- **Before baselines exist, nothing is drawn as expected.** L3 needs 2–4 weeks
  of history (doc 28 Phase 3). The empty state says so and gives the first fit
  date; the UI never draws a flat band from thin data.
- **Ground truth that only the simulator has stays labelled.** Exclusions
  created from the fault-injection ledger are tagged "Injected fault
  (simulator)". A real site has no such source.

---

## 3. Contract deviations (collected)

Each item keeps the contract name and recommends a change. Docs 29, 30 and 33
make the decision.

**Resolution status (2026-10-21, doc 30 §15):**

| # | Status |
|---|---|
| D1 | Adopted: doc 33 task 1.17 ports `NumberInput` |
| D2 | Stands: frontend-only. Doc 30 §10 serves `rule_limit` bands with null `expected`/`lower`, which `bandRuns()` must handle (§7.1) |
| D3 | Adopted: topic `anomalies`, dotted names kept. Finding events also go to `device:{id}` (doc 30 §8) |
| D4 | Adopted: `alarm_id` filter (doc 30 §4.1); `incident_id` filter added too |
| D5 | Adopted: `reason` required for dismiss (`422 reason_required`) and rejected on any other action (doc 30 §4.3) |
| D6 | Adopted: plus `severity_cap`, `l5_major_enabled`, `budget_per_hour`, `detail` (doc 30 §11) |
| D7 | Adopted: `interval`, repeatable `metric`, `{items}`, `reason` + `first_fit_due`; parameters keep `from`/`to` (doc 30 §10) |
| D8 | Adopted: `POST /incidents/{id}/explain/feedback` (doc 30 §7.2) |
| D9 | Adopted: `param_schema` with `default` (doc 30 §6.1) |
| D10 | Adopted as `POST /incidents/{id}/feedback`, scheduled for Phase 2 (doc 30 §5.4). The UI spec is §5.6 "Incident actions" |
| D11 | **Resolved; the interim rule is dropped.** `alarm.state` is authoritative. Acknowledging an alert-lane finding acknowledges its alarm in the same transaction. An alarm acknowledgement reaches the finding within one sweep (doc 30 §4.4) |

The original recommendations follow:

| # | Contract says | Code shows | Recommendation |
|---|---|---|---|
| **D1** | "NumberInput for numeric inputs" | **No `NumberInput` exists in dcim-platform `frontend/`.** It lives only in the simulator (`Datacenter_Network_Simulator/webui/src/components/NumberInput.tsx`, 80 lines). The platform uses raw `type="number"` inputs (`settings/components/PolicyEditor.tsx:142`, `assets/parts/PartList.tsx:202`, `assets/reservations/ReservationList.tsx:214-224`) | Port it verbatim to `frontend/src/components/NumberInput.tsx` with the same props (`value, onChange, fallback, int, min, max, step, style, className, placeholder, title, disabled, onFocus, onBlur`). Use it in the new anomaly surfaces only; migrating the existing inputs is out of scope |
| **D2** | "Device page Telemetry tab gains expected band via `components/Plot.tsx` band" | The Telemetry tab draws **`TimeChart`**, not `Plot` (`DeviceHistory.tsx:5, 175, 200`). `Plot`'s `band` *does* take a time-varying `[x, lower, upper]` series (`Plot.tsx:45-50, 154-160`). However, it has no bucket-gap breaking and no time axis. It is also unsafe with gaps: `Plot.tsx:107-109` pushes `lo` and `hi` into the y-range after checking only `x`, so a single `NaN` bound makes `Math.min(...ys)` `NaN` and collapses the whole chart | Give `TimeChart` an optional `band` (§7.1) and leave `Plot.tsx` untouched. A shared `components/band.ts` (`bandRuns()`) builds the band path. If `Plot` is ever used for a gapped band, it must adopt `bandRuns()` and filter out non-finite bounds first |
| **D3** | WS events `anomaly.finding.opened` … (dotted) | Every existing frame is snake_case (`alarm_created`, `alarm_updated`; `docs/11-websocket-spec.md:77-107`, `backend/app/alarms/service.py:65`), and the contract names no topic | Keep the contract names; the UI matches exact strings, so either form works. Doc 30 must name the **topic**. This doc assumes `anomalies`; the hub accepts any string (`backend/app/websocket/hub.py:95-104`). Add a note in doc 11 that the dotted form is deliberate |
| **D4** | `GET /findings` filters: state, lane, kind, detector, device_id, datacenter, room, since | The alarm evidence panel must find the finding behind a derived alarm. The `alarm` row has an `attributes` jsonb (`alembic/versions/0005_alarms_and_events.py:99`), but `raise_alarm()` takes no attributes argument (`repositories/alarms.py:217-225`) and the `Alarm` client type carries no such field (`api/client.ts:1753-1789`) | Add an **`alarm_id`** filter to `GET /findings`; the finding row already carries `alarm_id` (doc 28's table). No alarm schema change is needed |
| **D5** | `POST /findings/{id}/feedback` `{action, note?}` | Dismissal is how detector precision is measured (doc 28 "Advisories live in their own queue"), and free text cannot be counted | Add an optional **`reason`**: `not_a_fault` \| `expected_operation` \| `sensor_fault` \| `duplicate`. The UI requires it for `dismiss` and sends it with no other action |
| **D6** | `GET /health`: last sweep time, duration, findings/hour, lock holder | The UI must know whether detection is on (`DCIM_ANOMALY_ENABLED`) and which explainer is configured. Without that it cannot **hide** the AI panel under `explainer = none` short of probing for a 409 | Add `enabled: boolean`, `explainer: 'none'\|'cloud'\|'local'`, `sweep_s` and `baseline_last_fit_at` to `/health`. Viewers must be allowed to read it |
| **D7** | `GET /expected ?device_id&metric&instance&from&to` | `/history` chooses its own bucket and reports it as `interval` (`api/client.ts:2379-2384`). A band at a different bucket does not line up with the line it overlays. One call per series also means about 10 calls for a CRAH | (a) Add `interval` to `/expected`. The UI fetches the band *after* history resolves and passes `HistoryOut.interval`. (b) Let `metric` repeat, or be omitted to mean every modelled series, and return `{items: ExpectedBand[]}`. (c) When no baseline exists, return 200 with `points: []` and a `reason`, not 404, so the empty state can say *why* |
| **D8** | Explainer "thumbs feedback" (doc 28 §LLMs, control 6) | No endpoint in the contract | Add `POST /incidents/{id}/explain/feedback {useful: boolean, note?}`. Until it exists, the thumbs control is not rendered, so there are no dead buttons |
| **D9** | `GET /detectors` (enabled, params) | The editor needs bounds, units and defaults to render `NumberInput`s honestly. The `params` jsonb carries values only | Return `param_schema: ParamSpec[]` for each detector (§16). A param without a spec renders read-only |
| **D10** | Feedback is per finding; incidents have only `explain` | With 30 symptoms in one incident and no bulk acknowledge, an operator has to click 30 times | No incident-level feedback in this phase. The UI does **not** loop N requests client-side, because a half-applied loop is the failure-report problem described in doc 22 §4. Recommend `POST /incidents/{id}/feedback` for the backlog in doc 33 |
| **D11** | Alert-lane findings also exist as derived alarms | Two acknowledgements of the same condition, one on the finding and one on the alarm, will drift apart | Doc 30 decides whether a finding `acknowledge` is mirrored to `POST /alarms/{id}/acknowledge`. **Until it is**, an alert-lane row shows the alarm's own Ack button (through the existing alarm API) and hides the finding's Acknowledge. Confirm fault, Dismiss and Reopen stay on the finding. **Superseded by doc 30 §4.4; see the status table above** |

---

## 4. Shared vocabulary and encoding

Each concept has one visual form, used everywhere it appears: triage, detail,
device, alarm and settings.

| Concept | Values | Encoding (never hue alone) |
|---|---|---|
| **Kind** | `process`, `sensor`, `undetermined` | Glyph plus word. *Process* is a filled circle, *sensor* an outlined diamond (a probe), *undetermined* an outlined circle with a centre dot. All three are inked in `--text-muted`. Kind is a classification, not a state, so it gets no saturation |
| **Lane** | `alert`, `advisory` | *Alert*: the existing `StatusChip` with its severity word (`components/StatusChip.tsx:4-11`; MINOR/WARNING map to `--warn`), plus a link glyph to the alarm. *Advisory*: a neutral outlined chip, "Advisory", in `--text-muted`. **Advisories never use `--critical` or `--major`.** Saturation is spent only on state that needs a response |
| **State** | `open`, `acknowledged`, `confirmed`, `dismissed`, `resolved` (incident: `open`, `resolved`) | The word in the State column. `confirmed` adds a check glyph; `dismissed` is struck through in `--text-faint`; `resolved` is muted. No fills |
| **Verdict** (doc 29 D3) | `confirmed`, `dismissed`, or null. It survives resolution | A resolved finding with a verdict reads "Resolved · was dismissed" or "Resolved · was confirmed". That is what the precision figure counts |
| **Resolution** | `cleared`, `device_retired`, `detector_disabled`, `muted`, `superseded`, `manual` (doc 29 `ck_anomaly_finding_resolution`) | A muted word after "Resolved", using the copy keys `resolution.*` (§10) |
| **Tier** | `power`, `plant`, `loop`, `unit`, `zone`, `server`, `room` (doc 29 `ck_anomaly_incident_tier`) | Word: Power · Plant · Loop · Unit · Zone · Server · Room. `unit` is a CRAH or CDU, and `room` is the fallback used before CRAH zones are fitted |
| **Layer** | L0, L2, L3, L4 (L5 correlator, L6 explainer) | Mono tag such as `L2`, with the layer name in a tip. Shown in the detail and in settings, not in triage rows |
| **Detector** | Doc 28 IDs exactly: `CR-1`, `CH-2`, `CD-6`, `RM-3` … | Mono ID plus the catalogue name ("CRAH supply over setpoint"). The ID is what tickets and the design doc use, so it is always visible |
| **Alarm type** of a derived alert | Doc 29 §9.3 snake_case names: `crah_supply_over_setpoint`, `chiller_chw_over_setpoint`, `rack_hot_spot` … | In the alarm list it renders through `humanise()` as it does today. The evidence panel shows the detector ID and name beside it, because the alarm type is not the detector ID (that lives in `alarm.attributes.anomaly.detector_id`) |
| **Impact** | `impact_kw` estimate | "≈ 12 kW" in tabular numerals. Null renders "—" with the tip "No estimate for this detector". The column-header tip says *estimated* |
| **Deviation** | observed − expected | Signed, with the unit: "+3.4 K". The score follows in muted ink, formatted by layer: "4.1σ" for residual detectors, "held 18 min" for L2 rules |

Severity dots in facet cells reuse `.facet-seg .sw.warning` / `.minor`
(`estate.css:450-458`).

---

## 5. AnomalyView: triage (`/analytics?view=anomaly`)

### 5.1 Placement inside Analytics

`Analytics.tsx` changes in three places:

- `Tab` (`:24`) gains `'anomaly'`.
- `TABS` (`:26-33`) gains `{ key: 'anomaly', label: 'Anomalies', blurb: 'What
  the detectors noticed below alarm thresholds, grouped by likely cause' }`. It
  is placed **last**, so the existing tab order that people have learned does
  not shift.
- The room toolbar (`:71-86`) and the `!room` gate (`:98-109`) are skipped
  for this tab. Every other view answers a question about *one room*, whereas
  triage is estate-wide and has its own Site and Room filters. A room picker
  that silently scoped the queue would hide findings. Render
  `{tab === 'anomaly' && <AnomalyView />}` outside the `room` conditional.

### 5.2 Layout at 1280 (about 1422 CSS px after zoom)

```
Analytics
What the detectors noticed below alarm thresholds, grouped by likely cause
 Capacity  Forecast  PUE  Thermal  Cooling plant  Power  [Anomalies]
────────────────────────────────────────────────────────────────────────────────────────────
Anomalies                          ┌ OPEN INCIDENTS ┬ OPEN FINDINGS ┬ SENSOR HEALTH ┬ NEW IN 24 H ┐
Sweep 38 s ago · 41 ms · 6/h       │       3        │      27       │       5       │      9      │
                                   └────────────────┴───────────────┴───────────────┴─────────────┘
[ INCIDENTS 3 | FINDINGS 27 | SENSOR HEALTH 5 ]                          [ 24H | 7D | 30D | ALL ]
STATE [ALL | OPEN 21 | ACKNOWLEDGED 4 | CONFIRMED 2 | DISMISSED · | RESOLVED ·]
LANE  [ALL | ● ALERT 6 | ADVISORY 21]
[All sites ▾] [All rooms ▾] [All detectors ▾] [All device types ▾]  [Search this page     ]
┌──────────────────────────────────────────────────────────────┬──────────────────────────────┐
│ ▸ CRAH-B-03 · DC1 · Hall B                                   │  AnomalyDetail (§6)          │
│   CR-1 Supply over setpoint, valve 98% — CHW-starved         │  sticky, 460 px              │
│   Unit · 4 members · Alert MINOR · ≈ 34 kW · 12 min          │                              │
│ ▾ CHILLER-1 · DC1 · Plant                                    │                              │
│   CH-2 COP differs from expected                             │                              │
│   ├ ● CH-2  CHILLER-1     COP −0.62 · 2.8σ    Advisory  open │                              │
│   ├ ● CT-1  TOWER-1       approach +1.9 K     Advisory  open │                              │
│   └ ◇ RM-4  R2-07 inlet   +2.1 K vs aisle     Advisory  open │                              │
│ …                                                            │                              │
├──────────────────────────────────────────────────────────────┤                              │
│ 1–25 of 27 · page 1 of 2   Rows per page [25 ▾]  Go to [  ]  « ‹ 1 2 › »                    │
└──────────────────────────────────────────────────────────────┴──────────────────────────────┘
```

- **Header.** The title "Anomalies" sits on the left and `KpiBand`
  (`components/estate.tsx:24`) on the right of the **same row**, and the title
  shrinks first (the estate-head rule). The four tiles are numbers read on
  every visit:
  - *Open incidents*.
  - *Open findings* (process and undetermined, not sensor).
  - *Sensor health* (open findings with `kind = sensor`).
  - *New in 24 h*.

  The budget figure (findings per day against the advisory budget) is a number
  to decide on, so it belongs on the settings page, not here. Under the title
  is one muted line from `/health`: "Sweep 38 s ago · 41 ms · 6 findings/h". It
  becomes the stale banner (§5.7) when the sweep is late.
- **View strip.** A `Seg` (`components/estate.tsx:65-83`) with three cells, each
  carrying its count: `INCIDENTS`, `FINDINGS`, `SENSOR HEALTH`. It is held in
  `?av=incidents|findings|sensor`, and the default is `incidents`.
- **Range.** A `Seg` on the right of the title row, `24H | 7D | 30D | ALL`, which
  maps to `since`. *This deviates from the chart keys (30D/90D/180D/1Y), on
  purpose:* raw telemetry is kept for 30 days and findings are triaged over hours
  to weeks, so quarter- and year-long ranges have nothing to show.
- **Counts source.** One call, `GET /anomalies/findings?with_facets=true`
  (doc 30 §4.1), returns the list page together with `facets`. That object
  has the `AnomalySummary` shape and feeds both the tiles and the facet counts.
  Its counts cover the same filters minus `state`, so every State cell keeps
  a true count when another state is pressed. The API's default `state` is
  `open`, `acknowledged` and `confirmed`, so the State strip opens with those
  three pressed.
- **Facets.** `.seg.facet-seg` strips for State and Lane, with ALL first and
  counts inside. Kind is not a facet, because the view strip already separates
  sensor findings from the rest. The Findings view adds a third strip,
  `PROCESS | UNDETERMINED`.
- **Dimension selects** use the `.asset-panel-filters` values (26px, 0.76rem,
  gap 6, max-width 200): Site; Room (its options follow Site, and clearing the
  parent clears the child); Detector (grouped by layer with `<optgroup>`);
  Device type. Search filters device names on the current page only, because
  the contract has no text filter. Its placeholder says "Search this page".
- **Every filter writes to the URL**: `av`, `since`, `state`, `lane`, `kind`,
  `dc`, `room`, `detector`, `dtype`, `q`, `page`, `size`, `finding`,
  `incident`. A filtered queue is then a link someone can paste into a ticket.
- **Split.** `.anom-split` is a namespaced copy of the `.alarm-split` pattern
  (`index.css:1253-1271`), with a 460 px detail column instead of 380 so a
  440-wide chart keeps its native tick size (§6.3). Below 1100 px the list
  hides while the detail is open, the same rule as the alarm list.

### 5.3 Incidents view (default)

There is one row per incident, and its members expand beneath it.

| Column | Content | Sort |
|---|---|---|
| (toggle) | `▸`/`▾` disclosure button with `aria-expanded` | — |
| Root | Root device link, then the location `DC1 · Hall B` | ✓ |
| Diagnosis | `title` ("CHW supply loss: CH-DC1-02 tripped"), with `root_ref` (detector ID or alarm type) in mono, then the template `summary`. **Never the AI text** | — |
| Tier | The root's tier word (§4): Power, Plant, Loop, Unit, Zone, Server, Room | ✓ |
| Members | "17 · 6 alarms", with the sensor count and `suppressed_symptoms` on hover | ✓ |
| Lane | `lane`: Alert if any member is an alarm, otherwise Advisory | ✓ |
| Impact | `impact_kw` | ✓ **default, descending** |
| Opened | `opened_at` relative, absolute on hover; `last_activity_at` in the tip | ✓ |
| State | `open` or `resolved` | — |

**Default sort**: impact descending, then `opened_at` descending, then `id`.
This is a **total order** (doc 30 §5.1), so server paging stays stable. Null
impact sorts last.

**Expanded members** come from the detail endpoint's flat `members[]` (doc 30
§5.2), indented by `parent_id`. They render as nested rows showing the
member's `device_name`, `ref`, `role`, `state` and `severity`/`lane`, without
fetching each finding. The expansion shows up to 10, then a `usePaged` footer
inside it (`.sub-wrap .asset-pager`). The root (`is_root`) comes first and is
marked "root". `type: 'alarm'` members render a `StatusChip` and link to
`/alarms/:id`. `type: 'inferred'` members render their `label` in muted
italics, with no link.

Findings outside any incident still appear in the Findings view. The Incidents
view ends with a line, not a row: "12 findings are not part of any incident —
show them". The link switches to the Findings view.

### 5.4 Findings and Sensor health views

| Column | Default | Notes |
|---|---|---|
| Kind | ✓ | Glyph and word (§4). Hidden in Sensor health, where every row is a sensor |
| Detector | ✓ | `CR-4` in mono with the name, truncated, full name in a tip |
| Device | ✓ | Link to `/devices/:id?tab=telemetry`, with the instance after a `·` (`CHW`, `COND`) |
| Location | ✓ | `DC1 · Hall B · R2` |
| Deviation | ✓ | "+3.4 K · 4.1σ" (§4) |
| Impact | ✓ | "≈ 12 kW" |
| Lane | ✓ | Chip |
| State | ✓ | Word |
| Last seen | ✓ | Relative |
| First seen | chooser | |
| Occurrences | chooser | |
| Incident | chooser | Link, e.g. "INC · CHILLER-1", that opens the incident |
| Actions | ✓ | The row's primary feedback action (§5.6) |

The column chooser is saved per viewer in `localStorage`. Reads and writes are
wrapped in try/catch, and the defaults apply on failure.

**Default sort.** Findings: impact descending, then last seen descending, then
`id`. Sensor health: **last seen descending**, because a sensor fault has no
meaningful kW and "what broke most recently" is the maintenance question.

**Stale findings.** A finding open for more than 24 h with no feedback shows a
small "stale" label after its state. Doc 28 says to surface it, not re-raise
it. The label is a word, not a colour.

### 5.5 Row behaviour

- A row is `role="button"` with `tabIndex=0`, and Enter or Space opens the
  detail. This is the alarm-row pattern (`AlarmList.tsx:172-182`). The selected
  item lives in the URL (`&finding=<id>` / `&incident=<id>`), as the alarm list
  does, so a selection can be shared.
- The selected row gets `.is-selected` (`index.css:1266`).
- Links inside a row stop propagation (`AlarmList.tsx:184`).

### 5.6 Feedback actions

There are four actions with fixed names, identical in the row, the detail pane
and the alarm evidence panel. Using them needs `atLeast('operator')`
(`api/client.ts:30`). Viewers see the state but no buttons.

The actions follow doc 30 §4.3's state machine, which the server enforces with
`409 illegal_transition` and its `allowed` set:

| Action | Offered in state | Button | Dialog | Result |
|---|---|---|---|---|
| `acknowledge` | open | **Acknowledge** | none | acknowledged |
| `confirm` | open, acknowledged | **Confirm fault** | Optional note. Body: "Confirming counts this finding as a real fault in CR-4's precision." A `process` finding adds: "Its window is also kept out of the next baseline fit." | confirmed |
| `dismiss` | open, acknowledged | **Dismiss** | The **reason picker** below, plus an optional note (≤ 1000 characters) | dismissed |
| `reopen` | acknowledged, confirmed, dismissed | **Reopen** | none | open |

**The reason picker.** A `role="radiogroup"` labelled "Why is this not a
fault?". It is required: **Dismiss** stays disabled until a reason is chosen,
and the server returns `422 reason_required` if one is missing. The reason is
sent only with `dismiss`, because the server rejects it on other actions with
`422 reason_not_allowed`.

| `reason` | Label | Hint under the label |
|---|---|---|
| `not_a_fault` | Not a fault | The equipment is fine; the detector is wrong here. |
| `expected_operation` | Expected operation | Planned work, a setpoint change, staging or a test explains it. |
| `sensor_fault` | Sensor problem | The reading is wrong, not the plant. Fix or recalibrate the sensor. |
| `duplicate` | Duplicate | Another finding or alarm already covers this condition. |

The dismiss body reads: "Dismissing counts against CR-4's precision. It does
not mute the detector — to stop it on this device, add an override in
Settings." A second line follows: "The finding stays dismissed until the
condition clears; it will not re-raise meanwhile."

**Alert-lane findings (doc 30 §4.4).** The alarm is authoritative, so
**Acknowledge** is shown on alert-lane findings exactly as on advisories.
Pressing it acknowledges the finding and its alarm together, and the button's
tip says so: "Also acknowledges the alarm." An acknowledgement made on the
alarm, from the alarm list or a Jira ticket, reaches the finding within one
sweep. Until then the row reads "Alarm acknowledged · syncing". Confirm fault
and Dismiss never clear the alarm. The detail pane keeps the alarm's own
**Clear manually** off this surface: it stays on the alarm. Reopening a
finding leaves its alarm acknowledged, and the pane says: "The alarm stays
acknowledged."

- **A row offers one primary action**: Acknowledge while the finding is open,
  otherwise nothing. Confirm fault, Dismiss and Reopen live in the detail pane,
  because confirming and dismissing are judgements made after reading the
  evidence, not from a list row.
- Dialogs use `features/assets/components/Dialog.tsx` (focus trap, Escape,
  focus restored on close). It is imported, not forked.
- While pending, the button reads "Acknowledging…", "Confirming…",
  "Dismissing…" or "Reopening…" and is disabled.
- On error, a `.banner` shows "Could not dismiss: {message}". `ApiError.message`
  is always a sentence (`api/client.ts:55-66`). The row is not updated
  optimistically; the list refetches on success.
- A resolved finding offers no actions. It shows "Resolved {time} ·
  {resolution}" using the `resolution.*` copy, followed by the verdict if one
  survived ("· was dismissed").
- A `409` refetches the finding and shows "This finding changed since you
  opened it — it is now {state}."

**Incident actions (Phase 2, doc 30 §5.4).** Once
`POST /incidents/{id}/feedback` ships, the incident detail header offers
**Acknowledge symptoms**, **Dismiss…** and **Confirm fault…**, each with a
scope select: *Symptoms* (the default), *All findings* or *Sensor findings*.
Reopen is never offered in bulk. Dismiss uses the same reason picker. The
result is always shown as the row-level report — "9 acknowledged · 2 skipped
(already acknowledged)", with the skipped rows listed — never as a toast. Bulk
actions touch member findings only. A note in the dialog says so: "Alarms in
this incident are not changed, apart from alert-lane alarms acknowledged
through their findings." Until the endpoint ships, the header has no bulk
actions, and the UI never loops single requests.

### 5.7 States

| State | What renders |
|---|---|
| Loading | Eight `.asset-skeleton` rows in the table's shape, and skeleton tiles |
| Error | `.banner` with "Could not load anomalies: {message}" and a **Retry** button |
| Detection off (`health.enabled = false`, D6) | Replaces the table: "Anomaly detection is off on this platform." Admins also see: "Set DCIM_ANOMALY_ENABLED=true and restart the ingest workers. Findings appear after the first sweep." Others see: "An administrator can turn it on." |
| Sweep stale (more than 3 × `sweep_s` since the last sweep) | `.banner` above the facets: "The detector sweep last ran 14 min ago. New findings are delayed." The list still renders |
| Empty, no filters | "Nothing needs a look. The detectors ran 38 s ago and found no open conditions." Quiet is the correct default |
| Empty, with filters | Names the filter excluding everything: "No findings in Hall B in the last 24 h. Clear the room filter", where the last part is a link |
| Empty Sensor health | "Every sensor the detectors watch is reading plausibly." |
| Empty Incidents with findings present | "No findings are grouped into an incident. 9 ungrouped findings — show them" |

---

## 6. AnomalyDetail: a finding or an incident

The detail is a pane in the `.anom-split` right column at 1100 px and wider,
and replaces the list below that (§13). It is **not** a modal, because a
finding is read alongside its siblings. Its height is
`calc(100vh / var(--dz) - 120px)`; it is sticky and scrolls internally.

### 6.1 Section order (top to bottom)

Evidence comes before narrative, and the narrative comes last.

1. **Header.** Detector ID and name, device link, instance, kind glyph, lane
   chip and state. An "Open device" link goes to
   `/devices/:id?tab=telemetry&finding=<id>`. For an incident, the header shows
   the root device, the tier and the member count instead.
2. **Summary.** The deterministic template sentence from `evidence.summary`
   (`analytics/evidence.py`), headed **Summary**. It always renders, with or
   without an explainer.
3. **Expected and observed** chart (§6.3).
4. **Numbers**, as a `dl.kv`: Observed, Expected (with its range), Deviation,
   Score, Onset, First seen, Last seen, Occurrences, Impact (estimated), and
   Detector version (e.g. `CR-1 v3`).
5. **Contributing series** (§6.4).
6. **Incident tree** (§6.5). For a finding, it shows the incident the finding
   belongs to. For an incident, it is the main content.
7. **Evidence** (§6.6).
8. **Feedback history.** Who did what and when, with the reason and note,
   newest first, followed by the action buttons (§5.6).
9. **AI explanation** (§6.7). Incidents only, and only when
   `health.explainer ≠ 'none'`.

### 6.2 Selecting

`&finding=<id>` fetches `GET /anomalies/findings/{id}`, and `&incident=<id>`
fetches `GET /anomalies/incidents/{id}`. If the item is not on the current
page, the pane still loads it and says "Not in the current view — shown because
you arrived with a direct link" (copied from `AlarmList.tsx:233-238`). A 404
shows "No finding with that id. It may have been purged by retention, or the
link may be truncated." with **Back to the list**.

### 6.3 Expected and observed chart

- **Component**: `TimeChart` with `band`, `marks` and a measured `width`
  (§7.1). It is not `Plot`, because this is telemetry (rulebook form 6).
- **Window**: from `onset − 2 h` (falling back to `first_seen − 2 h`) to
  `last_seen + 30 min`, clamped to now, and at most 7 days. A `Seg`
  `FIT | 6H | 24H | 7D` overrides it; FIT is this default.
- **Series**: the finding's `(metric, instance)` as the observed line in slot 1.
  The band and the expected line take the same slot colour (§7.1).
- **Marks**: a shaded x-span from onset to `last_seen` (or to now), labelled
  "Finding" at the top-left of the span.
- **Basis line** under the chart, muted: "Expected from CH-2 v2, fitted on 21
  days to 14 Oct (n = 504 h), ±3σ." It uses `ExpectedBand.fit_from`, `fit_to`,
  `n` and `k`.
- **L2 rules** have no regression baseline. `/expected` serves them as
  `kind: 'rule_limit'` (doc 30 §10), with `expected = null` and only the bounds
  the rule defines. For CR-1 that is `upper = setpoint + over_setpoint_k`,
  which moves with the setpoint. `TimeChart` draws a one-sided band as a
  **limit line** rather than a fill: 1.25 px in the series colour, dashed
  `5 4` (the rulebook's reference vocabulary), labelled "Rule limit" at the
  right. The dwell is the mark span. This works from Phase 1.
- **L0 sensor findings** chart the sensor together with the coupled variable
  the detector conditioned on. For flatline-while-process-moves, for example,
  that is CRAH supply temperature with the CHW valve. Those are different
  units, so they are two stacked charts, never two axes.
- **Chrome**: `.asset-panel` with the h3 "Expected and observed", the maximize
  glyph, and `MaxModal` rendering the same children at full width.
- **Empty**: the band item has `points: []` and a `reason`, and the sentence
  comes from the `band.reason.*` copy (§7.2 item 6).
- **Basis line by kind**: `ols` and `profile` bands also print `r2`, for example
  "R² 0.81". Points before `fit_from` are shaded with the mark token and
  labelled "before fit". When `sigma_source` differs from `interval`, the
  basis line adds "(σ from 5-min fit)".

### 6.4 Contributing series

These are the correlated-series *summaries* from the evidence bundle: min, max
and slope over a window, never raw series (doc 28 explainer control 2). Each
renders as a compact row:

> evidence ID chip · device · metric · instance · "min 18.2 · max 24.9 ·
> slope +1.1 K/h over 2 h"

Each row has a 120×28 sparkline (the existing `components/Sparkline.tsx`) with
its range printed beneath. Clicking a row adds that series as its own chart
below §6.3, on its own frame and never on a shared axis. Five rows show at
first, then "Show all 12". The source is `evidence.items` where
`type = 'series'`.

### 6.5 Incident tree

A `role="tree"` list built from the flat `members[]` (doc 30 §5.2) by
`parent_id`. It runs along the doc 29 tiers, `power` → `plant` → `loop` →
`unit` → `zone` → `server`, with `room` as the fallback.

```
INCIDENT · opened 07:58 · root CH-DC1-02 (Plant) · chiller_trip
▾ Plant   CH-DC1-02        A1  chiller_trip                       MAJOR  ACTIVE   root
  ▾ Loop  CHW loop A (inferred from cooling edges)
    ▾ Unit CRAH-DC1-HA-03  F3  CR-1 Supply over setpoint          Alert  open     symptom
      ▸ Zone SEN-DC1-HA-R2-04  A4  inlet_temp_high                WARNING ACTIVE  context
    ▸ Unit CRAH-DC1-HA-05  F5  CR-1 …                             Alert  open     symptom
Also considered: PU-DC1-CHW-02 — proved running throughout
Context: X1 No fire pre-discharge in Hall A
```

- **Members.** Each node shows its `evidence_id` chip, `device_name` (or
  `label` for `type: 'inferred'`), `ref` (detector ID or alarm type), its
  severity or lane, its `state`, and its `role`, worded as root, symptom or
  context. `context` members are related and inside the window but not
  suppressed. Every L1 alarm is `root` or `context`, never `symptom`
  (doc 30 §5.2).
- **Opening a member.** A finding member opens with `&finding=<id>` (fetched
  on open), an alarm member links to `/alarms/:id`, and an inferred member is
  not interactive.
- **Keyboard.** Items are `role="treeitem"` with `aria-expanded` and
  `aria-level`. Up and Down move between items, and Left and Right collapse and
  expand (the WAI-ARIA tree pattern).
- **Inferred nodes** use the server's `label`, for example "CHW loop A (inferred
  from cooling edges)". The CHW loop has no entity of its own (doc 28). Before
  Phase 4 fits CRAH zones, the grouping falls back to `tier: 'room'`, which
  reads "Hall B (room-level grouping)".
- **Context items** (fire pre-discharge, a maintenance window, standby
  rotation) are listed under the tree with the word "Context". They are what the
  correlator used to *explain away* a symptom, and an operator has to see them
  to trust the suppression.
- **Root ranking**: if the correlator weighed several roots, one line,
  "Also considered: …", names them with the server's `reason` for each
  (`also_considered[]`).

### 6.6 Evidence list

Every item in `evidence.items`, in bundle order. Each shows its **ID as a mono
chip** (`A1`, `F3`, `T1`, …) exactly as the bundle assigned it; the UI never
renumbers. The one-line form for each type:

| type | Line |
|---|---|
| `alert` | `A1` chiller_chw_over_setpoint MINOR on CHILLER-1 · expected ≤ 7.5 °C · observed 9.1 °C · onset 14:02 |
| `finding` | `F4` CT-1 TOWER-1 approach +1.9 K (2.4σ) since 11:40 |
| `series` | Rendered in §6.4; listed here by ID only |
| `edge` | `T1` CHW-LOOP-2 → CRAH-B-03 (cooling) |
| `setpoint_change` | `C1` CRAH-B-03 air setpoint 22 → 21 °C at 13:30 by bms |
| `maintenance` | `M1` Window "Tower 1 fill inspection" 09:00–12:00 · link |
| `similar_incident` | `H1` 2026-09-14 · CHILLER-1 · CH-1 · resolved in 2 h · link |
| `doc` | `D1` SOP or OEM snippet title · link |

ID prefixes come from doc 30 §5.3:

| Prefix | Item |
|---|---|
| `A` | alert or alarm |
| `F` | finding |
| `S` | series |
| `T` | topology edge |
| `C` | setpoint change |
| `M` | maintenance |
| `H` | history |
| `D` | doc snippet |
| `X` | context |

IDs are stable for a given `bundle_sha256`, and the footer prints the first 8
characters of that hash, so a pasted evidence list can be matched to its
bundle. A finding's own evidence (`evidence.signals[]`, doc 30 §4.2) uses
`S1…` IDs in the same way.

**Copy evidence** copies the bundle as plain text with its IDs, ready for a
ticket. After 12 items the list collapses to "Show all 31".

### 6.7 AI explanation

**When `health.explainer === 'none'`, this section does not exist.** There is
no heading, no button, no placeholder and no "AI is off" text, because the
system is fully functional without it (contract policy). If the explainer is
switched off after the page has loaded, `POST /explain` returns 409. The pane
then refetches `/health` and the section disappears without an error.

When the explainer is enabled:

- The section is a `<details className="anom-ai">`, **collapsed by default**.
  Its open state is remembered per viewer in `localStorage`, with try/catch.
- The summary line reads **"AI-generated explanation"**, followed by the muted
  "candidates to check, not findings". It carries an "AI" text badge (an
  outlined chip in `--text-muted`, not the accent), so the label survives
  greyscale and screen readers.
- **Not generated yet** (`has_explanation = false`): an **Explain this
  incident** button for operators and above. While pending: "Explaining… this
  can take up to a minute." The response is the doc 30 §7.1 `Explanation`.
  - A `409 explain_in_progress` shows "Someone else is generating this
    explanation" and polls the incident after `Retry-After`.
  - A `429 explain_rate_limited` shows "Explanations are rate-limited. Try again
    in {n} min."
- **The label comes from the server.** `explanation.label` ("AI-generated:
  candidates to check, not a diagnosis") is rendered verbatim as the summary
  line's sub-label, so the wording is owned in one place.
- **Model output** (`source = 'llm'`, `fallback_used = false`) renders
  `explanation.output`:
  - *Summary* paragraph.
  - *Hypotheses*, as an ordered list: the text, "model's confidence 70%", then
    the cited evidence chips under **Supports** and **Contradicts**. Each chip
    is a button that scrolls to and focuses that evidence row (§6.6).
  - *Checks to perform*, as a read-only list.
  - *Unknowns*.
  - A muted footer: "Model {model} · {explainer} · generated {generated_at} ·
    temperature 0 · bundle {bundle_sha256:8}".
- **Fallback** (`fallback_used = true`, `source = 'template'`). There is no AI
  text to show, so the panel shows one muted sentence chosen by
  `validator.rejected_reason`, followed by "The summary above is generated
  from the rules." No partial text is ever shown.

  | `rejected_reason` | Sentence |
  |---|---|
  | `validation_failed` | The explanation was withheld because it cited evidence that is not in the bundle. |
  | `schema_invalid` | The explanation was withheld because it did not follow the required format. |
  | `timeout` | The explainer did not answer in time. |
  | `explainer_error` | The explainer returned an error. |

  **Try again** sends `force: true`.
- **Stale** (`stale = true`, meaning the bundle changed after the text was
  written): "Written before the incident changed — evidence may differ.
  Explain again". The link sends `force: true`.
- **Thumbs** (doc 30 §7.2): "Was this useful? **Yes** / **No**" sends
  `POST /incidents/{id}/explain/feedback {useful}`. The response's counts
  appear beside the buttons ("3 · 1"), and the viewer's own vote is pressed
  (`aria-pressed`). A vote belongs to the current `explanation_sha256`, so a
  re-explanation starts at zero.
- The text renders as **plain text**, never as HTML or Markdown. It contains
  device names and ticket text that came from outside the platform, which doc 28
  says to treat as data, as a guard against prompt injection.

---

## 7. Device Telemetry: expected-range overlay

### 7.1 The `TimeChart` change, and why not `Plot`

`Plot`'s `band` already supports a time-varying lower/upper series
(`PlotBand.points: [x, lower, upper][]` at `Plot.tsx:45-50`; the path is built
from per-point bounds at `:154-160`). It is still the wrong component here:

1. The Telemetry tab draws `TimeChart` (`DeviceHistory.tsx:175, 200`).
   `TimeChart` breaks lines across collection gaps longer than three buckets
   (`TimeChart.tsx:48, 146-156`), formats time ticks by span, and gives the tip
   the data's precision. `Plot` does none of this.
2. `Plot`'s band is one closed polygon, so it bridges an excluded window or a
   missing hour.
3. A single non-finite bound breaks the whole chart, because `Plot.tsx:107-109`
   pushes `lo` and `hi` into the y-range after checking only `x`.
4. The band never reaches the tooltip, and its legend entry appears only when
   there are two or more series (`Plot.tsx:287, 308-313`).

**The change** goes in `components/TimeChart.tsx`. It is entirely optional, and
the render is unchanged when the props are absent.

```ts
// components/band.ts (new)
export interface TimeBand {
  /** `${metric}|${instance}` of the series the band belongs to; the band takes
   *  that series' slot colour. */
  seriesKey: string;
  /** Legend and tip label: "Expected range". */
  label: string;
  /** Sigma multiple, printed in the legend: "(±3σ)". */
  k: number;
  /** [epoch_ms, lower, expected, upper], each possibly null (doc 30 §10).
   *  A null triple is a gap and ends the run. */
  points: [number, number | null, number | null, number | null][];
  /** 'rule_limit' bands have expected = null and one or both bounds; they
   *  draw as limit lines, not a fill. */
  kind?: string | null;
}
/** Split into runs at non-finite bounds and at gaps > gapMs, and return one
 *  closed path per run. Used by TimeChart, and by Plot if it ever adopts it. */
export function bandRuns(points: TimeBand['points'], gapMs: number,
  sx: (v: number) => number, sy: (v: number) => number): string[];

// components/TimeChart.tsx — the signature at :113 becomes
export function TimeChart({ series, unit, bucketMs, band, marks, width = 720 }: {
  series: Series[]; unit: string; bucketMs: number;
  band?: TimeBand;
  /** Shaded x-spans: a finding's onset..last_seen. */
  marks?: { from: number; to: number | null; label: string }[];
  /** viewBox width, 720 by default. A 440 pane keeps 10px ticks legible. */
  width?: number;
})
```

Rendering rules:

- **Order**: grid → marks → band fill → expected line → series lines →
  crosshair and tip → hit rect. The band sits under the line it describes.
- **Null handling**: a point whose bounds are all null ends the run. A run where
  only one bound is non-null (`rule_limit`, or a one-sided model) draws that
  bound as a limit line instead of a fill. A null `expected` draws no expected
  line.
- **Band fill**: the banded series' slot colour at opacity **0.16** (rulebook
  form 7). It uses the series colour rather than `projection`
  (`seriesColors.ts:58`), so on a three-line panel the band visibly belongs to
  its own line.
- **Expected line**: the same colour, 1.25 px, `strokeDasharray="2 3"`
  (dotted). A plain `6 4` dash means "projected" (`Plot.tsx:238-243`), and an
  expectation is not a projection. The 4th series slot is also dotted `2 3`
  (`seriesColors.ts:48`), so a panel that would need a 4th line gets no band.
  Curated panels never go past three lines. A `groupByUnit` panel with four or
  more lines renders without a band and with the caption "Expected range is
  shown on panels of up to three lines."
- **Y-range** includes the finite band bounds. A fault far outside the band must
  be in the same frame as the band, because that distance is what the reader is
  looking at.
- **Marks**: a `--anom-mark` fill (a new token, §14) over the full plot height,
  a 2 px top rule in `--text-muted`, and the label in `.chart-tick-x` at the
  top-left. A span with no end runs to the last x.
- **Tooltip**: when the band has a point within `bucketMs / 2` of the hovered x,
  add the row "Expected 21.4 (20.1–22.7) °C". The band is modelled, so a
  half-bucket snap is acceptable for it. The **observed** rows still snap only
  to real samples (`TimeChart.tsx:171-182`).
- **Legend** renders when `series.length > 1 || band`. The band's swatch is a
  9 px square at 0.35 opacity with a dotted line through it, labelled "Expected
  range (±3σ)".
- **aria-label** gains ", with the expected range from {detector}" when a band
  is present.
- **`width`**: below 520, the x ticks thin to two (start and end). PAD_L stays
  52.

### 7.2 `DeviceHistory.tsx` changes

```
History            [1 h | 6 h | 24 h | 7 d | 30 d] [Refresh]   [Expected range: Findings ▾]   to 14:32
2 open findings on this device — CR-4 filter loading (advisory, 3 days), CR-1 supply over
setpoint (alert, 12 min). View in triage →
┌ UNDER WATCH · FILTER DIFFERENTIAL ────────────────────────────────────────────────────────┐
│  charted because CR-4 is open; not on this device's curated panels                         │
└────────────────────────────────────────────────────────────────────────────────────────────┘
┌ AIR TEMPERATURES ──────────────────────────────────────────────────────────────────────────┐
│  supply ──  return ─·─  setpoint ···   ░ expected range (±3σ)   ▒ finding                  │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

1. **Findings line.** Fetch
   `GET /anomalies/findings?device_id=<id>&state=open&state=acknowledged`. If
   it returns nothing, no line renders and the tab looks as it does today. If it
   returns findings, one muted sentence above the charts names up to three of
   them (detector name, lane, age) and links "View in triage →". With one
   finding the link goes to `&finding=<id>`; with more, to the Findings view
   filtered to this device.
2. **Under watch panels.** A finding's `(metric, instance)` may not be on any
   curated panel. The chiller group lacks `cop` and `approach_temp` (CH-2,
   CH-3), and the CRAH group lacks `filter_diff_pressure` (CR-4)
   (`features/devices/chartGroups.ts:64-71, 86-92`). Those metrics are added to
   the history fetch, and each gets its own panel at the **top** titled "Under
   watch · {metric label}". Without this, the overlay would have nowhere to draw
   for exactly the slow faults L3 exists to catch.
3. **Band source.** After history resolves, call `/expected` with the history's
   `interval` (D7a). The band mode decides which series are asked for:
   - **Findings** (default): only series with an open or acknowledged finding.
   - **All modelled**: every charted series. With D7b this is one call; without
     it, one call per series, capped at 12.
   - **Off**.

   The mode is a `<select>` placed outside the Range group, as Refresh is
   (`DeviceHistory.tsx:126-134`), so it does not read as an eighth range. It is
   held in `?band=findings|all|off`.
4. **One band per panel.** On a multi-line panel, the band goes on the series
   with the finding, or else on the first modelled series in the panel's
   declared order.
5. **Marks** are drawn for every open finding on the series.
6. **States.** While the band loads, nothing extra is shown: the line is already
   drawn, and a skeleton over a drawn chart flickers. On error, a muted caption
   under the panel reads "Expected range unavailable: {message}". An item with
   `points: []` gets a caption chosen by its `reason` (doc 30 §10). The same
   sentences are used in the detail chart (§6.3).

   | `reason` | Caption |
   |---|---|
   | `no_baseline` | No expected range: no detector models this series. |
   | `insufficient_history` | No expected range yet — baselines need about two weeks of history. First fit is due {first_fit_due}. With no date: "First fit is due once enough history has built up." |
   | `detector_disabled` | No expected range: {detector} is switched off. Admins also see a link: "Turn it on in Settings" |
   | `refused` | No expected range: the last fit rejected this series as too thin. It is retried at the next nightly fit. |
   | `feature_disabled` | No expected range: anomaly detection is off on this platform. |

   In **All modelled** mode, `no_baseline` is the normal case for most series,
   so its caption is suppressed there and shown only in the detail chart.
7. **Live updates.** Finding frames are also published to `device:{id}`
   (doc 30 §8). `DeviceHistory` therefore adds no `anomalies` subscription.
   It uses
   `useTopics(['device:' + id])` (already held by the device page, and the hub
   de-duplicates) together with
   `useInvalidateOn(FINDING_EVENTS, [['device-findings', id]])`. The **charts
   do not advance** (the existing rule, `DeviceHistory.tsx:65-77`). Only the
   findings line and the marks follow the socket; the band follows Refresh.
8. A `?finding=<id>` param, from the detail's "Open device" link, scrolls to the
   panel carrying that finding and outlines it briefly: 2 px `--accent` for
   1.2 s, with no animation under `prefers-reduced-motion`.

`DeviceDetail.tsx` is not edited. It mounts `DeviceHistory` as before
(`DeviceDetail.tsx:127-132`), and the new fetches live inside `DeviceHistory`.

---

## 8. Alarm detail: evidence panel for derived alarms

### 8.1 Where

The panel goes inside `AlarmDetailBody`
(`features/alarms/AlarmDetail.tsx:16-57`), after the `dl.kv`. `AlarmDetailBody`
is shared by the alarm list pane (`AlarmList.tsx:239`) and the home alarm modal
(`AlarmDetail.tsx:66`, opened from `features/home/AlarmPanel.tsx:943`). Both get
the panel, which is the reason the body is shared: two copies drift
(`AlarmDetail.tsx:9-15`). Every alarm that is not
`detection === 'derived' && source === 'analytics'` renders exactly as it does
today.

### 8.2 Content

```
── Evidence ────────────── Raised by analysis · CR-1 CRAH supply over setpoint v3
Expected ≤ 24.0 °C (setpoint 21 + 3 K) · observed 26.8 °C · held 18 min
CHW valve 98% — the rule reads this as CHW-starved, not a control fault
[ supply_air_temp with the moving rule limit (rule_limit band) and the finding span ]   ⤢
Part of incident: CH-DC1-02 (Plant) · 17 members                 Open incident →
S1 …  S2 …  (first 4 evidence lines)                             All evidence →
[Confirm fault] [Dismiss]     (Acknowledge is the alarm's own button above; the finding follows it)
```

The alarm's heading stays `humanise(alarm.alarm_type)`, for example
`crah_supply_over_setpoint` → "Crah supply over setpoint". That is today's
behaviour and is not changed. The panel's sub-heading carries the detector ID
and its proper name. A severity line is added when `/health.severity_cap` is
set: "Derived alerts are capped at {severity_cap}." When the root's alarm was
escalated by L5 (`l5_major_enabled`), the line instead reads "Escalated to
MAJOR by the cooling correlator: {n} racks affected under one root." That
tells the reader why an analytics alarm reads MAJOR.

In this pane, **Acknowledge** is the alarm's existing button (in the home
modal, `AlarmDetail.tsx:117-121`). Because the alarm is authoritative
(doc 30 §4.4), the finding follows within one sweep, so the panel adds no
second Acknowledge.

- **Lookup**: `GET /anomalies/findings?alarm_id=<alarm.id>` (D4), then
  `GET /anomalies/findings/{id}`.
- **Chart**: `TimeChart width={348}` in the 380 px list pane and `width={720}`
  in the home modal (`.modal` is `min(1080px, 100%)`, `estate.css:314-318`).
  Both have the maximize glyph.
- **Incident**: one line linking to `/analytics?view=anomaly&incident=<id>`.
- **Evidence**: the first four lines in the §6.6 format, then **All evidence →**
  linking to the finding in triage. Before Phase 3 there is no triage tab, so
  the control is **Show all evidence** and expands the list in place.
- **No AI text here.** The alarm pane is where an operator decides what to do in
  the next few minutes, and the narrative belongs in triage. This is the one
  place doc 28's "L6 narrative below it when enabled" is narrowed, because a
  380 px pane cannot carry it without pushing the evidence off-screen.
  > **Contract deviation (vs doc 33 task 5.5).** Task 5.5 lists
  > `features/alarms/AnomalyEvidence.tsx` among the files for
  > `AiExplanation`. This doc recommends that the alarm panel carry only a
  > link, "AI explanation available →" (shown when `has_explanation` is true
  > and the explainer is not `none`), which opens the incident in triage. Task
  > 5.5 should drop `AnomalyEvidence.tsx` from its file list.
- **Incident tree** (Phase 2, doc 33 task 2.7): the one-line incident link
  expands into the §6.5 tree, collapsed by default, before the full triage tab
  exists in Phase 3. Until Phase 3, "Open incident →" is not rendered, because
  there is no triage tab to open.
- **States**: three skeleton lines while loading. When the finding is missing:
  "Raised by analysis, but its finding record was not found — it may have been
  purged by retention." On a lookup error, a `.banner` shows the message, and the
  alarm fields above still render.

### 8.3 Detection filter on `AlarmList`

Add one `<select aria-label="Detection">` after the severity select
(`AlarmList.tsx:143-149`), with the options *Any detection*, *Threshold*,
*State*, *Absence*, *Derived (analysis)* and *Forecast*. It is held in the URL as
`?detection=`, so the evidence panel and triage can link to "All derived
alerts". It is a select, not a facet strip, to match its neighbours on this
toolbar. The page still sends `limit: '200'` with no pager
(`AlarmList.tsx:49`), which predates the table rule. That is **not** changed
here because it is out of scope; it is noted for doc 33.

---

## 9. Settings: `AnomalyDetectors` (`/settings/anomaly-detectors`)

### 9.1 Placement

- `SETTINGS_NAV` (`features/settings/SettingsLayout.tsx:9-55`) gains
  `{ to: 'anomaly-detectors', label: 'Anomaly detectors', blurb: 'Which checks
  run, how sensitive they are, and what they ignore' }`. It goes after
  **Ticketing**, since both decide what reaches people, and before Appearance.
- `App.tsx:363-375` gains
  `<Route path="anomaly-detectors" element={<AnomalyDetectors />} />`.
- Operators and above can read the page; only **admins** can edit it
  (`atLeast('admin')`, following `features/alarms/AlarmRules.tsx:42`).
  Viewers see it read-only with "Changing detectors needs an administrator."

### 9.2 Layout

```
Anomaly detectors
Which checks run, how sensitive they are, and what they ignore.
Sweep 38 s ago · 842 ms · ingest-1:31 · 2 findings/h (budget 4/h) · baselines fitted 03:00
Derived alerts capped at MINOR · MAJOR escalation off · explainer none
[ DETECTORS 38 | OVERRIDES 4 | EXCLUSIONS 7 ]
LAYER [ALL | L0 6 | L2 15 | L3 13 | L4 4]   [All device types ▾]  [Search ID or name    ]
┌──────┬──────────────────────────────┬──────┬─────────────┬─────────────┬─────┬──────┬──────────┬─────────┐
│ ID   │ Name                         │Layer │ Applies to  │ Lane        │ Ver │ Open │ Judged   │ Enabled │
├──────┼──────────────────────────────┼──────┼─────────────┼─────────────┼─────┼──────┼──────────┼─────────┤
│ CR-1 │ CRAH supply over setpoint    │ L2   │ CRAH (28)   │ Alert MINOR │ v3  │  2   │ 9 of 10  │  [✓]    │
│ ▾ Parameters                                                                                           │
│    Supply margin over setpoint   [ 3.0 ] K    default 3.0 · 0.5–10                                     │
│    Valve saturation              [ 95  ] %    default 95                                               │
│    Dwell                         [ 900 ] s    default 900                                              │
│    Requires supply_air_temp, air_setpoint_temp, valve_position_pct{CHW}, equipment_state                │
│    Inactive on 0 devices (missing points)                                [Revert]  [Save as v4]         │
└────────────────────────────────────────────────────────────────────────────────────────────────────────┘
1–25 of 38 · page 1 of 2 …
```

The health lines come from `/health` (doc 30 §11). The first carries the
advisory budget (`budget_per_hour`; doc 28 sets it at "tens of new findings per
day estate-wide"). When findings per hour have run over budget for 24 h, the
line adds "over budget — tune the noisiest detector", linking to the detector
table sorted by open findings.

The second line states the two policy switches in words, so an operator never
has to infer them:

- `severity_cap` → "Derived alerts capped at MINOR".
- `l5_major_enabled` → "MAJOR escalation on" or "MAJOR escalation off". The
  switch itself is the `L5` row's `major_enabled` param, the only way to turn
  it on (doc 30 §6.2).

The line is followed by the explainer mode. When `last_sweep_at` is null, the
line reads "No sweep in the last 10 min". `detail.sweep.errors_1h > 0` adds
"{n} sweep errors in the last hour", with `last_error` in a tip.

### 9.3 Detectors table

| Column | Notes |
|---|---|
| ID | Mono; doc 28 IDs exactly |
| Name | From the catalogue |
| Layer | Mono `L2`, with the layer name in a tip |
| Applies to | Device types and the number of devices in scope |
| Lane | The detector's default output: Alert (with its severity word and its `alarm_type` in mono in a tip, e.g. `crah_supply_over_setpoint`) or Advisory |
| Version | `v3`. Rule sets need version numbers; even G36 keeps being corrected (doc 28) |
| Open | Open findings, linking to triage filtered by `detector` |
| Judged | Confirmed out of confirmed plus dismissed over the current version, printed "9 of 10". It shows "—" when `precision` is null or n < 5. It is a count rather than a percentage so that a small n is visible |

**The `L5` row** (doc 33 D13) is the cooling correlator. It is listed last
under its own group heading, "Correlator". Its params are switches: *Escalate
roots to MAJOR*, *Use fitted CRAH zones*, *Correlation window*, *Racks for
escalation*. Saving it **never bumps the version** (doc 30 §6.2), so its Save
button reads "Save" rather than "Save as v{n}". Turning on *Escalate roots to
MAJOR* asks for confirmation first: "Derived roots with confirmed impact will
raise MAJOR alarms that reach the alarm console and Jira policy. The Phase 2
gate in doc 33 should pass first."
| Enabled | Checkbox switch. Turning a detector off asks first: "Disable CR-1? Its {n} open findings are resolved as detector disabled, and their derived alerts clear. Nothing new is raised until you turn it back on." (doc 30 §6.2) |

The default sort is catalogue order (layer, then ID). Open and Judged are
sortable. The table pages with `Pagination`.

### 9.4 Parameter editing

- A row expansion (`aria-expanded`) holds a form generated from `param_schema`
  (D9).
- `kind: 'number' | 'int'` renders a **`NumberInput`** (D1) with `min`, `max`
  and `step`. `int` is set for counts and seconds. The unit follows the field,
  and "default X · min–max" follows in muted ink. A changed value shows a dot
  glyph and the word "changed", not a colour alone.
- `kind: 'bool'` renders a checkbox and `kind: 'enum'` a `<select>`.
- Out-of-range values are checked on blur and show an inline error under the
  field: "Must be between 0.5 and 10 K." **Save** stays disabled while any field
  is invalid.
- **Save** is labelled "Save as v4", so the version bump is visible before it
  happens. A parameter change bumps the version; toggling Enabled does not
  (doc 30 §6.2). Save opens a confirm dialog titled "Save CR-1 as v4?" that
  lists each change ("Over setpoint 3.0 → 2.5 K"), then reads:

  > Saving starts version 4. The detector re-learns its running totals, so
  > slow-drift findings may take hours to reappear.
  >
  > Open findings from v3 stay open and resolve normally. If this change makes
  > their accumulated evidence meaningless — for example a new CUSUM k or h —
  > they are resolved as **superseded**, and new findings open under v4 if the
  > condition is still there.
  >
  > Precision restarts at v4: confirmed and dismissed counts are per version.

  The buttons are **Save as v4** and **Cancel**. *Realism:* changing a
  CUSUM's k or h really does invalidate its accumulated state, so the reset
  is correct, not a side effect to hide. Confirming sends
  `PATCH /detectors/{id} {params}`.
  - `422 param_out_of_range` or `unknown_param` puts the server's message under
    the named field.
  - A response with `"changed": []` closes the dialog with "Nothing changed".
- **Revert** restores the last saved values without a request.
- L3 and L4 rows also print the baseline basis, e.g. "Baseline fitted 03:00, 21
  days, n = 504 h · 3 series refused (thin buckets)".

### 9.5 Overrides tab

Per-device sensitivity or mute. This mirrors `alarm_rule_override` (doc 28's
table).

| Column | Notes |
|---|---|
| Device | Link |
| Detector | ID and name |
| Effect | "Muted", or the changed params, e.g. "margin 3.0 → 4.0 K" |
| Reason | Text. Required for a mute (`422 mute_needs_reason`) |
| Expires | `expires_at`, or "never". An expired override is shown muted with "expired — ignored" |
| Set by / when | `updated_by`, relative time |
| (action) | **Remove override**, with a confirm step, sends `DELETE /overrides/{detector_id}/{device_id}` |

**Add override** opens a `Dialog`:

1. **Device**, chosen with `features/assets/components/DevicePicker.tsx`
   (imported, single select).
2. **Detector**, filtered to those that apply to the chosen device's type.
3. Either *Mute on this device* or *Change parameters*. The latter is the same
   `NumberInput` form, pre-filled with the effective values.
4. **Reason**: required for a mute, recommended for a parameter change.
5. **Expires** (optional), an `<input type="datetime-local">`.

The dialog footer says: "Muted detectors raise nothing on this device, and its
open {detector} findings are resolved as muted. Prefer an exclusion window for
planned work." An override that sets nothing is refused (`422 override_noop`),
so **Save** stays disabled until something differs from the effective values. Saving sends
`PUT /overrides/{detector_id}/{device_id}`. The table pages with `usePaged`.

### 9.6 Exclusions tab

An exclusion is a window **removed from baseline fits**. It does *not* silence
anything. Doc 28 is explicit that maintenance windows must be excluded from
baselines, "or the next refit learns the fault as normal".

The rows and fields follow doc 29 §4.3 and doc 30 §6.4.

| Column | Notes |
|---|---|
| Scope | Exactly one of device (link), room or site (`device_id`, `room_id`, `datacenter_id`), narrowed further by `detector_id` and/or `metric_key` when set ("CRAH-DC1-HA-03 · CR-4 · filter_diff_pressure") |
| Window | `starts_at` – `ends_at`, absolute, with the duration. A null `ends_at` reads "open — still excluding" |
| Source | *Manual*; *Maintenance window* (`maintenance`, `source_ref` links to `/assets/maintenance/:id`); *Confirmed fault* (`confirmed_finding`, `source_ref` opens the finding); *Injected fault (simulator)* (`injected_fault`, `source_ref` is the ledger id, e.g. `f-000193`) |
| Reason | Text |
| Created by | User name or "system" |
| (action) | **Delete**, for `manual` and `injected_fault` rows. `maintenance` and `confirmed_finding` rows are system-owned (`409 exclusion_system_owned`) and say "Managed by its source" instead |

A Source facet strip (ALL · MANUAL · MAINTENANCE · CONFIRMED FAULT · INJECTED)
and an *Active now* toggle map to `?source=` and `?active=`.

**Add exclusion** fields:

- Scope: a device, room or site select. Exactly one is allowed; the others
  disable when one is picked.
- Optional detector and metric.
- *Starts* and *Ends* (`<input type="datetime-local">`, 26px). *Ends* may be
  left empty: "leave empty to keep excluding until you close it".
- A required Reason.

The form posts `source: 'manual'`; injected rows come from campaign tooling,
not this form. Two errors are shown under their fields: `422 exclusion_scope`
and `422 exclusion_dates_invalid` ("End must be after start"). A note under the
form reads: "Excluding a window removes it from the next nightly fit at 03:00
UTC. It does not hide findings and does not change baselines already fitted."
The default sort is `starts_at` descending, and the table pages with
`usePaged`.

---

## 10. Copy deck

Strings are sentence case, except where existing classes impose uppercase
chrome (`.seg` cells, `.asset-panel h3`). Verbs are plain. **Each action has one
name, everywhere**: Acknowledge · Confirm fault · Dismiss · Reopen · Explain
this incident.

| Key | Text |
|---|---|
| tab.label | Anomalies |
| tab.blurb | What the detectors noticed below alarm thresholds, grouped by likely cause |
| head.title | Anomalies |
| head.sweep | Sweep {age} ago · {ms} ms · {n} findings/h |
| kpi.incidents | Open incidents |
| kpi.findings | Open findings |
| kpi.sensor | Sensor health |
| kpi.sensor.detail | open sensor problems |
| kpi.new | New in 24 h |
| view.* | Incidents · Findings · Sensor health |
| facet.state / facet.lane | State / Lane |
| lane.alert / lane.advisory | Alert / Advisory |
| kind.* | Process · Sensor · Undetermined |
| kind.tip.process | The equipment or process behaves differently from expected. |
| kind.tip.sensor | A reading disagrees with its peers or its physics. Check the sensor before the plant. |
| kind.tip.undetermined | Not enough redundant readings to tell a sensor problem from a process one. |
| lane.tip.advisory | An early warning for whoever schedules the work. Never pages. |
| lane.tip.alert | Also raised as an alert in the alarm list by a named rule. |
| state.* | Open · Acknowledged · Confirmed · Dismissed · Resolved |
| state.stale | stale |
| state.stale.tip | Open for more than 24 hours with no feedback. |
| col.impact.tip | Estimated extra kW, or kW at risk. An estimate, not a meter reading. |
| col.deviation.tip | Observed minus expected, and how unusual that is for this series. |
| action.acknowledge | Acknowledge |
| action.confirm | Confirm fault |
| action.dismiss | Dismiss |
| action.reopen | Reopen |
| action.pending.* | Acknowledging… · Confirming… · Dismissing… · Reopening… |
| action.error | Could not {verb}: {message} |
| confirm.body | Confirming counts this finding as a real fault in {detector}'s precision. |
| dismiss.body | Dismissing counts against {detector}'s precision. It does not mute the detector — to stop it on this device, add an override in Settings. |
| dismiss.body.2 | The finding stays dismissed until the condition clears; it will not re-raise meanwhile. |
| dismiss.reason.group | Why is this not a fault? |
| dismiss.reason.not_a_fault | Not a fault |
| dismiss.reason.not_a_fault.hint | The equipment is fine; the detector is wrong here. |
| dismiss.reason.expected_operation | Expected operation |
| dismiss.reason.expected_operation.hint | Planned work, a setpoint change, staging or a test explains it. |
| dismiss.reason.sensor_fault | Sensor problem |
| dismiss.reason.sensor_fault.hint | The reading is wrong, not the plant. Fix or recalibrate the sensor. |
| dismiss.reason.duplicate | Duplicate |
| dismiss.reason.duplicate.hint | Another finding or alarm already covers this condition. |
| dismiss.reason.required | Choose a reason. |
| confirm.body.process | Its window is also kept out of the next baseline fit. |
| ack.alert.tip | Also acknowledges the alarm. |
| ack.alert.syncing | Alarm acknowledged · syncing |
| reopen.alarm | The alarm stays acknowledged. |
| conflict | This finding changed since you opened it — it is now {state}. |
| note.label | Note (optional) |
| resolved.line | Resolved {time} · {resolution}{verdict} |
| resolution.cleared | condition cleared |
| resolution.device_retired | device retired |
| resolution.detector_disabled | detector disabled |
| resolution.muted | muted by an override |
| resolution.superseded | superseded by a new detector version |
| resolution.manual | resolved by hand |
| verdict.suffix | · was dismissed / · was confirmed |
| bulk.ack | Acknowledge symptoms |
| bulk.dismiss / bulk.confirm | Dismiss… / Confirm fault… |
| bulk.scope | Symptoms · All findings · Sensor findings |
| bulk.note | Alarms in this incident are not changed, apart from alert-lane alarms acknowledged through their findings. |
| bulk.report | {n} {verb} · {k} skipped ({why}) |
| empty.none | Nothing needs a look. The detectors ran {age} ago and found no open conditions. |
| empty.filtered | No findings {filter summary}. Clear the {filter} filter |
| empty.sensor | Every sensor the detectors watch is reading plausibly. |
| empty.incidents | No findings are grouped into an incident. {n} ungrouped findings — show them |
| ungrouped.line | {n} findings are not part of any incident — show them |
| off.title | Anomaly detection is off on this platform. |
| off.admin | Set DCIM_ANOMALY_ENABLED=true and restart the ingest workers. Findings appear after the first sweep. |
| off.other | An administrator can turn it on. |
| stale.banner | The detector sweep last ran {age} ago. New findings are delayed. |
| error.load | Could not load anomalies: {message} |
| retry | Retry |
| search.placeholder | Search this page |
| detail.direct | Not in the current view — shown because you arrived with a direct link. |
| detail.404 | No finding with that id. It may have been purged by retention, or the link may be truncated. |
| detail.back | Back to the list |
| detail.opendevice | Open device |
| detail.summary | Summary |
| chart.title | Expected and observed |
| chart.basis | Expected from {detector} v{ver}, fitted on {days} days to {date} (n = {n} h), ±{k}σ. |
| chart.rule | Rule limit |
| chart.prefit | before fit |
| chart.sigma | (σ from {sigma_source} fit) |
| band.reason.no_baseline | No expected range: no detector models this series. |
| band.reason.insufficient_history | No expected range yet — baselines need about two weeks of history. First fit is due {first_fit_due}. |
| band.reason.insufficient_history.nodate | No expected range yet — first fit is due once enough history has built up. |
| band.reason.detector_disabled | No expected range: {detector} is switched off. |
| band.reason.detector_disabled.admin | Turn it on in Settings |
| band.reason.refused | No expected range: the last fit rejected this series as too thin. It is retried at the next nightly fit. |
| band.reason.feature_disabled | No expected range: anomaly detection is off on this platform. |
| chart.mark | Finding |
| legend.band | Expected range (±{k}σ) |
| tip.expected | Expected {v} ({lo}–{hi}) {unit} |
| series.title | Contributing series |
| series.more | Show all {n} |
| tree.title | Incident |
| tree.inferred | (inferred from cooling edges) |
| tree.roomlevel | {room} (room-level grouping) |
| tree.context | Context |
| tree.also | Also considered: {list} |
| evidence.title | Evidence |
| evidence.copy | Copy evidence |
| evidence.more | Show all {n} |
| ai.title | AI-generated explanation |
| ai.sub | (server `explanation.label`) AI-generated: candidates to check, not a diagnosis |
| ai.button | Explain this incident |
| ai.pending | Explaining… this can take up to a minute. |
| ai.busy | Someone else is generating this explanation. |
| ai.ratelimited | Explanations are rate-limited. Try again in {n} min. |
| ai.fallback.validation_failed | The explanation was withheld because it cited evidence that is not in the bundle. |
| ai.fallback.schema_invalid | The explanation was withheld because it did not follow the required format. |
| ai.fallback.timeout | The explainer did not answer in time. |
| ai.fallback.explainer_error | The explainer returned an error. |
| ai.fallback.tail | The summary above is generated from the rules. |
| ai.stale | Written before the incident changed — evidence may differ. Explain again |
| ai.error | Could not generate an explanation: {message} |
| ai.retry | Try again |
| ai.hyp.conf | model's confidence {p}% |
| ai.supports / ai.contradicts | Supports / Contradicts |
| ai.checks / ai.unknowns | Checks to perform / Unknowns |
| ai.footer | Model {model} · {explainer} · generated {time} · temperature 0 · bundle {sha8} |
| ai.thumbs | Was this useful? Yes / No |
| ai.alarmlink | AI explanation available → |
| dev.findings | {n} open findings on this device — {list}. View in triage → |
| dev.underwatch | Under watch · {metric} |
| dev.bandmode | Expected range: Findings · All modelled · Off |
| dev.band.err | Expected range unavailable: {message} |
| dev.band.none | No expected range: {reason} |
| dev.band.cap | Expected range is shown on panels of up to three lines. |
| alarm.ev.title | Evidence |
| alarm.ev.by | Raised by analysis · {detector} v{ver} |
| alarm.ev.incident | Part of incident: {root} ({tier}) · {n} members · Open incident → |
| alarm.ev.all | All evidence → |
| alarm.ev.missing | Raised by analysis, but its finding record was not found — it may have been purged by retention. |
| alarm.ev.cap | Derived alerts are capped at {severity_cap}. |
| alarm.ev.escalated | Escalated to MAJOR by the cooling correlator: {n} racks affected under one root. |
| alarm.ev.showall | Show all evidence |
| health.cap | Derived alerts capped at {severity_cap} |
| health.l5 | MAJOR escalation on / MAJOR escalation off |
| health.nosweep | No sweep in the last 10 min |
| health.errors | {n} sweep errors in the last hour |
| alarm.detection.* | Any detection · Threshold · State · Absence · Derived (analysis) · Forecast |
| set.nav.label | Anomaly detectors |
| set.nav.blurb | Which checks run, how sensitive they are, and what they ignore |
| set.readonly | Changing detectors needs an administrator. |
| set.tabs | Detectors · Overrides · Exclusions |
| set.disable.title | Disable {id}? |
| set.disable.body | Its {n} open findings are resolved as detector disabled, and their derived alerts clear. Nothing new is raised until you turn it back on. |
| set.save | Save as v{next} (L5 row: Save) |
| set.revert | Revert |
| set.save.title | Save {id} as v{next}? |
| set.save.confirm | Saving starts version {next}. The detector re-learns its running totals, so slow-drift findings may take hours to reappear. |
| set.save.superseded | Open findings from v{cur} stay open and resolve normally. If this change makes their accumulated evidence meaningless — for example a new CUSUM k or h — they are resolved as superseded, and new findings open under v{next} if the condition is still there. |
| set.save.precision | Precision restarts at v{next}: confirmed and dismissed counts are per version. |
| set.save.noop | Nothing changed |
| set.l5.major.confirm | Derived roots with confirmed impact will raise MAJOR alarms that reach the alarm console and Jira policy. The Phase 2 gate in doc 33 should pass first. |
| set.range.err | Must be between {min} and {max} {unit}. |
| set.changed | changed |
| set.inactive | Inactive on {n} devices (missing points) |
| set.budget.over | over budget — tune the noisiest detector |
| ovr.add / ovr.remove | Add override / Remove override |
| ovr.mute / ovr.params | Mute on this device / Change parameters |
| ovr.note | Muted detectors raise nothing on this device, and its open {detector} findings are resolved as muted. Prefer an exclusion window for planned work. |
| ovr.expired | expired — ignored |
| exc.add / exc.delete | Add exclusion / Delete |
| exc.note | Excluding a window removes it from the next nightly fit at 03:00 UTC. It does not hide findings and does not change baselines already fitted. |
| exc.open | open — still excluding |
| exc.ends.hint | leave empty to keep excluding until you close it |
| exc.dates.err | End must be after start |
| exc.source.* | Manual · Maintenance window · Confirmed fault · Injected fault (simulator) |
| exc.managed | Managed by its source |
| live.changes | {n} changes since this list loaded. Refresh |
| thermal.link | Anomalies and early warnings → |

**Words not used**: "AI detected", "anomaly score" without a unit, "critical"
for any advisory, "failed" for an L3 or L4 finding, "Got it", "Snooze".

---

## 11. Live updates

- **Topics** (doc 30 §8):
  - `anomalies` carries finding and incident events. `AnomalyView`,
    `AnomalyDetail`, the alarm evidence panel and the settings health line
    subscribe to it.
  - `device:{id}` also carries finding events. `DeviceHistory` relies on it
    and does not subscribe to `anomalies`.
- **Events**, matched on the exact strings: `anomaly.finding.opened`,
  `anomaly.finding.updated`, `anomaly.finding.resolved`,
  `anomaly.incident.opened`, `anomaly.incident.updated`. They are one exported
  constant, `ANOMALY_EVENTS`, in `features/analytics/anomaly/anomalyEvents.ts`.
- **Refetch, never patch.** The socket has no replay (`ws/useSocket.ts:51-63`).
- **The detail pane is live.** It uses `useInvalidateOn(ANOMALY_EVENTS,
  [['anomaly-finding', id]])`, or `[['anomaly-incident', id]]` for an incident.
  A pane does not reorder, so updating it under the reader is safe.
- **The list is not reshuffled under the cursor**, because a triage row moving
  as someone reaches for Dismiss is how the wrong finding gets dismissed.
  Instead, frames increment a counter, and an `aria-live="polite"` bar says "4
  changes since this list loaded. Refresh". The list refetches on its own only
  when all three hold: it is on page 1, nothing in the table has focus or
  hover, and no dialog is open. Otherwise the bar waits for a click.
- **KPI tiles and facet counts** refetch on every frame. They come from the `with_facets=true` findings call, keyed `['anomaly-summary', …filters]`.
  A number changing does not move anyone's click target.
- **Reconnect.** When `useSocketStatus()` goes from `reconnecting` to `open`,
  invalidate every `anomaly-*` key. After a gap, REST is the source of truth
  (`ws/client.ts:1-6`).
- **Debounce.** Frames that arrive within 2 s of each other are coalesced into
  one invalidation, because a chiller trip opens dozens of findings in a single
  sweep.
- **Frame payloads are used for routing only.**
  - `change` (doc 30 §8) decides which keys to invalidate. `bulk`
    (`{count}`, above the 50-frame cap) invalidates every `anomaly-*` list.
    `explained` invalidates `['anomaly-incident', id]`.
  - `anomaly.finding.updated` with `via: "alarm"`, or an `alarm_updated`
    frame for an alarm the open pane shows, also invalidates `['alarms']` and
    `['alarm', id]`.
  - Payloads are never merged into the cache.

---

## 12. Accessibility

- **Focus.** Rows are focusable buttons that open with Enter or Space. Opening a
  detail moves focus to its `h3` (`tabIndex=-1`). Escape closes the detail and
  returns focus to the row, unless a maximized chart owns Escape (`maxOpen()`,
  `features/home/AlarmTrend.tsx:26`). Dialogs trap focus (`Dialog.tsx`).
  `:focus-visible` is suppressed only for pointer input (`index.css:1323`), so
  keyboard focus is always visible.
- **Keyboard.** All controls are native buttons, selects and inputs. The
  incident tree follows the WAI-ARIA tree pattern (§6.5). Disclosure rows use
  `aria-expanded` and `aria-controls`. `Seg` and facet cells use `aria-pressed`
  (`components/estate.tsx:76`). No single-key shortcuts are added, because they
  clash with screen-reader browse mode.
- **Colour is never the only channel.** Kind has a glyph and a word; lane has a
  chip with a word; state is a word; severity uses `StatusChip` text. The band
  is a fill, a dotted line and a legend entry; marks carry a label; changed
  params say "changed"; facet dots sit beside words.
- **Contrast.** Text that carries meaning uses `--text` or `--text-muted`.
  `--text-faint` is used only for facet counts (the existing pattern) and for
  dismissed rows, which still carry their state word. Non-text marks (glyphs,
  the band edge, the mark rule) must reach at least 3:1 against both `--bg` and
  `--bg-raised`. `scripts/validate_palette.js` checks the new tokens (§14).
- **Charts.** `role="img"` with a sentence label, for example: "Supply air
  temperature on CRAH-B-03 over the last 6 hours, in °C, with the expected range
  from CR-1." The numbers block (§6.1, item 4) is the text equivalent and comes
  right after the chart.
- **Live regions.** The "changes since loaded" bar is `aria-live="polite"`, as
  are action results such as "Dismissed". Errors use `role="alert"` banners.
- **Reduced motion.** The only animation is the 1.2 s panel outline (§7.2), and
  it is skipped under `prefers-reduced-motion: reduce`.
- **AI section.** It uses native `<details>` and `<summary>`. The "AI" badge is
  text, not an icon, so screen readers announce it.

---

## 13. Responsive behaviour

| Window width | Effective CSS px | Behaviour |
|---|---|---|
| ≥ 1700 | 1:1 | List and the 460 px detail side by side, with all default columns |
| 1280 (zoom 0.9, `index.css:1334-1336`) | ≈ 1422 | Same split. When the list column measures under 900 px (ResizeObserver, sizes through `toCss()`), Location and Last seen move to a second line in the Device cell |
| 1100–1279 | ≈ 1222–1421 | The split holds. Occurrences and First seen are available from the chooser only |
| < 1100 (no zoom) | 1:1 | `.anom-split.with-detail` goes single-column: the detail replaces the list, with **Back to the list** at its top (the alarms rule, `index.css:1267-1271`). KPI tiles tighten their padding before the band wraps (the estate-head rule) |
| < 860 | 1:1 | The settings nav becomes a horizontal strip (`index.css:906-910`). The detector table moves Applies to and Judged into the row expansion. Facet strips scroll horizontally in `overflow-x: auto` and never wrap onto three lines |
| ≤ 480 | 1:1 | 16 px gutters. Triage rows render as two-line cards (kind glyph and detector on the first line; device, deviation and lane on the second), keeping table semantics through `display: grid` on `tr`. The pager collapses to ‹ page ›, with the range text kept |

A chart never widens its container (`min-width: 0; contain: inline-size`, per
the rulebook). In the detail pane, `TimeChart` gets its `width` from the
measured pane width, rounded to 20 px, so its ticks stay at native size.

---

## 14. Light and dark tokens

The existing tokens are used: `--bg`, `--bg-raised`, `--bg-inset`, `--border`,
`--border-strong`, `--text`, `--text-muted`, `--text-faint`, `--accent`, `--ok`,
`--warn`, `--major`, `--critical` and `--series-1..4`. They are defined at
`index.css:7-72` for dark, `:220-288` for `[data-theme="light"]`, and in the
`prefers-color-scheme: light` mirror at `:289+`.

Two new tokens are **added** to all three blocks:

| Token | Dark | Light | Use |
|---|---|---|---|
| `--anom-mark` | `color-mix(in srgb, var(--text-muted) 10%, transparent)` | `color-mix(in srgb, var(--text-muted) 12%, transparent)` | Finding span on charts |
| `--anom-ai-edge` | `var(--border-strong)` | `var(--border-strong)` | Left rule on the AI `<details>`, so it reads as a separate voice |

No new hue is introduced. Band colours come from the series slot through
`useChartColors()`. `validate_palette.js` runs in CI and must still pass. It
must list `--anom-mark` so that its contrast check confirms every series line
stays at least 3:1 against the mark fill.

SVG fills use `style={{ fill }}` or values resolved by `useChartColors()`, never
`fill="var(--x)"`. Nothing concatenates hex alpha; translucency uses
`color-mix`.

---

## 15. Component and file list

```
frontend/src/
  components/
    NumberInput.tsx              NEW   port of simulator webui/src/components/NumberInput.tsx (D1)
    band.ts                      NEW   TimeBand + bandRuns() (D2)
    TimeChart.tsx                EDIT  optional band / marks / width (§7.1)
  features/analytics/
    Analytics.tsx                EDIT  tab, toolbar skip (§5.1)
    AnomalyView.tsx              NEW   triage: head, view Seg, facets, table, pager, split
    AnomalyDetail.tsx            NEW   pane: summary, chart, numbers, series, tree, evidence, feedback, AI
    anomaly.css                  NEW   .anom-* only
    anomaly/
      FindingRow.tsx             NEW   row and nested member rows
      IncidentTree.tsx           NEW   role=tree
      EvidenceList.tsx           NEW   ID chips, copy
      ContributingSeries.tsx     NEW
      ExpectedChart.tsx          NEW   TimeChart + band + marks + basis line + MaxModal
      AiExplanation.tsx          NEW   <details>; not rendered when explainer = none
      FeedbackActions.tsx        NEW   the four actions and their dialogs (Dialog.tsx imported)
      KindGlyph.tsx              NEW   three glyphs, aria-hidden, word beside
      LaneChip.tsx               NEW
      anomalyEvents.ts           NEW   ANOMALY_EVENTS, topic, query keys
      format.ts                  NEW   deviation / score / impact formatters
  features/devices/
    DeviceHistory.tsx            EDIT  findings line, Under watch panels, band mode (§7.2)
  features/alarms/
    AlarmDetail.tsx              EDIT  evidence panel in AlarmDetailBody (§8)
    AnomalyEvidence.tsx          NEW   the panel itself
    AlarmList.tsx                EDIT  detection select (§8.3)
  features/settings/
    SettingsLayout.tsx           EDIT  nav entry
    AnomalyDetectors.tsx         NEW   health line, tabs, detector table
    anomaly/
      ParamForm.tsx              NEW   param_schema → NumberInput / checkbox / select
      OverridesTab.tsx           NEW
      ExclusionsTab.tsx          NEW
  features/estate/Thermal.tsx    EDIT  (optional) one sub-link
  App.tsx                        EDIT  settings route
  api/client.ts                  EDIT  additive types and methods (§16)
```

These are reused without edits: `Pagination.tsx`/`usePaged`, `estate.tsx`
(`Seg`, `KpiBand`), `MaxModal.tsx`, `HoverTip.tsx`, `StatusChip.tsx`,
`Sparkline.tsx`, `seriesColors.ts`,
`features/assets/components/{Dialog,DevicePicker}.tsx`, `ws/useSocket.ts`,
`lib/format.ts`, `lib/zoom.ts`.

**Tests**. These **depend on doc 33 Q15**: there is no test runner in
`frontend/package.json` today. If vitest is not added, §18 lists the fallback
checks.

- A `TimeChart` snapshot without `band` that is byte-identical to before, and
  one with a gapped band that produces two runs and no `NaN` in any `d`.
- `bandRuns`, including null triples and one-sided `rule_limit` runs.
- `ParamForm` range validation.
- `FeedbackActions` visibility by state and by role.
- An `AnomalyView` URL round-trip for every filter.

---

## 16. `api/client.ts` additions (additive)

These go in one new section, `// ---- anomalies (docs 28-31)`, after the alarm
types (around `client.ts:2672`). The methods go in `export const api`
(`:3890`). The types below **include every doc 30 §13 correction**. Doc 30
remains authoritative for field names.

```ts
export type FindingState = 'open' | 'acknowledged' | 'dismissed' | 'confirmed' | 'resolved';
export type FindingVerdict = 'confirmed' | 'dismissed';
export type FindingResolution =
  | 'cleared' | 'device_retired' | 'detector_disabled' | 'muted' | 'superseded' | 'manual';
export type FindingKind = 'sensor' | 'process' | 'undetermined';
export type FindingLane = 'advisory' | 'alert';
export type IncidentState = 'open' | 'resolved';
export type FeedbackAction = 'acknowledge' | 'dismiss' | 'confirm' | 'reopen';
/** Required with `dismiss`, rejected otherwise (doc 30 §4.3). */
export type DismissReason = 'not_a_fault' | 'expected_operation' | 'sensor_fault' | 'duplicate';
/** 'L5' is the correlator switch row in the catalogue (doc 33 D13). */
export type DetectorLayer = 'L0' | 'L2' | 'L3' | 'L4' | 'L5';
/** doc 29 ck_anomaly_incident_tier */
export type Tier = 'power' | 'plant' | 'loop' | 'unit' | 'zone' | 'server' | 'room';

export interface AnomalyFinding {
  id: string;
  detector_id: string;               // 'CR-1'
  detector_name?: string | null;
  detector_version: number;
  layer: DetectorLayer;
  summary: string;                   // deterministic template text, never LLM
  device_id: string; device_name: string; device_type: string;
  metric_key?: string | null; instance: string; unit?: string | null;
  kind: FindingKind; lane: FindingLane; state: FindingState;
  verdict: FindingVerdict | null;    // survives resolution (doc 29 D3)
  severity?: string | null;          // alert lane only: the alarm's severity
  first_seen: string;                // condition began
  raised_at: string;                 // dwell satisfied, finding opened
  last_seen: string;
  occurrences: number; reopen_count: number;
  score?: number | null; score_unit?: 'sigma' | 'seconds' | 'ratio' | null;
  expected?: number | null; observed?: number | null;
  impact_kw?: number | null;
  stale: boolean;
  incident_id?: string | null; alarm_id?: string | null;
  datacenter_code?: string | null; room_id?: string | null;
  room_name?: string | null; rack_name?: string | null;
  resolved_at?: string | null; resolution?: FindingResolution | null;
}

export interface FindingFeedback {
  at: string; by: string;
  action: FeedbackAction; reason?: DismissReason | null; note?: string | null;
  from_state: FindingState; to_state: FindingState;
  detector_version: number;
  via: 'finding' | 'alarm';          // doc 30 §4.4
}

export type EvidenceType =
  | 'alert' | 'finding' | 'series' | 'edge' | 'setpoint_change'
  | 'maintenance' | 'similar_incident' | 'doc' | 'context';
export interface EvidenceItem {
  id: string;                        // 'A1' 'F3' 'S1' 'T1' 'C1' 'M1' 'H1' 'D1' 'X1'
  type: EvidenceType;
  text: string;                      // deterministic one-liner
  alarm_id?: string | null; finding_id?: string | null;
  device_id?: string | null;
  metric_key?: string | null; instance?: string | null;
  ts?: string | null; window?: { from: string; to: string } | null;
  summary?: { min: number; max: number; slope_per_h: number; unit: string; last?: number } | null;
  href?: string | null;
}
export interface EvidenceBundle {
  v: number;
  bundle_sha256: string;
  built_at: string;
  summary: string;
  devices: { id: string; name: string; type: string }[];
  items: EvidenceItem[];
}

/** GET /findings/{id}. `evidence` is the finding's own jsonb (doc 29 §7.4),
 *  not an incident bundle: signals[] carry the citable S-ids. */
export interface AnomalyFindingDetail extends AnomalyFinding {
  detector: { id: string; name: string; version: number; layer: DetectorLayer;
              params: Record<string, number | boolean | string> };
  evidence: {
    window: { from: string; to: string };
    rule?: { expr: string; dwell_s: number; params_version: number } | null;
    diagnosis?: string | null;
    expected?: number | null; observed?: number | null;
    lower?: number | null; upper?: number | null;
    signals: EvidenceItem[];
    gates?: Record<string, unknown>; masks?: Record<string, unknown>;
    baseline?: Record<string, unknown> | null;
    alarm_history?: Record<string, unknown>[];
  };
  rule_limit?: { value: number; label: string } | null;   // L2 only
  feedback: FindingFeedback[];
}

/** Flat; the tree is built from parent_id (doc 30 §5.2). The detail pane
 *  fetches the finding or alarm by id when a member is opened. */
export interface IncidentMember {
  type: 'finding' | 'alarm' | 'inferred';
  id: string;                        // finding id, alarm id, or 'loop:DC1-CHW-A'
  is_root: boolean;
  parent_id: string | null;
  tier: Tier;
  role?: 'root' | 'symptom' | 'context';
  inferred?: boolean; label?: string | null;
  device_id?: string | null; device_name?: string | null;
  ref?: string | null;               // detector id or alarm_type
  severity?: string | null; state?: string | null;
  lane?: FindingLane; kind?: FindingKind; alarm_id?: string | null;
  onset?: string | null; evidence_id?: string | null;
}
export interface AnomalyIncident {
  id: string; state: IncidentState; tier: Tier;
  title: string; summary: string;
  root_device_id: string; root_device_name: string;
  root_finding_id?: string | null; root_alarm_id?: string | null;
  root_ref: string;                  // detector id or alarm_type
  lane: FindingLane;
  opened_at: string; last_activity_at: string; resolved_at?: string | null;
  member_count: number; alarm_count: number; sensor_count: number;
  suppressed_symptoms: number;
  impact_kw?: number | null;
  datacenter_code?: string | null; room_name?: string | null;
  has_explanation: boolean;
}
/** doc 30 §7.1 / doc 29 §7.5. "status" is derived: fallback_used +
 *  validator.rejected_reason. */
export interface Explanation {
  v: number;
  source: 'llm' | 'template';
  label: string;                     // rendered verbatim
  explainer: 'cloud' | 'local';
  model: string; temperature: number;
  bundle_sha256: string; prompt_sha256: string;
  output: {
    summary: string;
    hypotheses: { text: string; confidence: number; supporting: string[]; contradicting: string[] }[];
    checks: string[]; unknowns: string[];
  };
  validator: {
    ok: boolean;
    rejected_reason: 'timeout' | 'explainer_error' | 'schema_invalid' | 'validation_failed' | null;
    errors: string[];
  };
  fallback_used: boolean;
  explanation_sha256: string;
  generated_at: string;
  stale: boolean;
}
export interface AnomalyIncidentDetail extends AnomalyIncident {
  members: IncidentMember[];
  context: EvidenceItem[];           // X-ids: fire, maintenance, standby
  also_considered: { device_name: string; reason: string }[];
  evidence: EvidenceBundle;
  explanation?: Explanation | null;
  explanation_feedback: { at: string; by: string; vote: 'up' | 'down';
                          explanation_sha256: string; note?: string | null }[];
}
export interface ExplainResponse { incident_id: string; cached: boolean; explanation: Explanation }

export type ExpectedReason =
  | 'no_baseline' | 'insufficient_history' | 'detector_disabled' | 'refused' | 'feature_disabled';
export interface ExpectedBand {
  device_id: string; metric_key: string; instance: string; unit: string;
  detector_id: string; detector_version: number;
  kind: string | null;               // 'profile' 'ols' 'slope' 'curve' 'first_order' 'peer' 'rule_limit'
  k: number; interval: string; sigma_source?: string | null;
  fit_from?: string | null; fit_to?: string | null; fitted_at?: string | null;
  n?: number | null; r2?: number | null;
  /** [epoch_ms, lower, expected, upper]; any may be null. */
  points: [number, number | null, number | null, number | null][];
  reason: ExpectedReason | null;     // set when points is empty
  first_fit_due?: string | null;
  alternatives?: { detector_id: string; kind: string }[];
}

export interface ParamSpec {
  key: string; label: string; unit?: string | null;
  kind: 'number' | 'int' | 'bool' | 'enum';
  min?: number; max?: number; step?: number; options?: string[];
  default: number | boolean | string; help?: string;
}
export interface AnomalyDetector {
  id: string; name: string; version: number; layer: DetectorLayer;
  alarm_type: string | null;         // doc 29 §9.3 snake_case, e.g. 'crah_supply_over_setpoint'
  device_types: string[]; required_metrics: string[];
  devices_in_scope: number; inactive_devices: number;
  enabled: boolean;
  lane: FindingLane | null; severity?: string | null;
  params: Record<string, number | boolean | string>;
  param_schema: ParamSpec[];
  open_findings: number; confirmed: number; dismissed: number;
  precision: number | null;
  baseline?: { fitted_at: string; days: number; n: number; refused: number } | null;
}
export interface DetectorPatchResult {
  ok: boolean; detector_id: string; version: number; changed: string[]; detector: AnomalyDetector;
}
export interface AnomalyOverride {
  detector_id: string; device_id: string; device_name: string;
  enabled: boolean; params: Record<string, number | boolean | string>;
  reason?: string | null; expires_at?: string | null;
  updated_by: string; updated_at: string;
}
export type ExclusionSource = 'maintenance' | 'injected_fault' | 'confirmed_finding' | 'manual';
export interface AnomalyExclusion {
  id: string;
  device_id?: string | null; device_name?: string | null;
  room_id?: string | null; room_name?: string | null;
  datacenter_id?: string | null;
  detector_id?: string | null; metric_key?: string | null;
  starts_at: string; ends_at: string | null;
  source: ExclusionSource; source_ref?: string | null;
  reason: string; created_by: string;
}
export interface AnomalyHealth {
  enabled: boolean;
  explainer: 'none' | 'cloud' | 'local';
  sweep_s: number;
  last_sweep_at: string | null; last_sweep_ms: number | null;
  findings_per_hour: number; budget_per_hour: number | null;
  lock_holder: string | null;        // 'ingest-1:31'
  baseline_last_fit_at: string | null;
  severity_cap: string;              // 'MINOR'
  l5_major_enabled: boolean;
  detail: {
    sweep: { p95_ms_1h: number; runs_1h: number; gate_skips_1h: number; errors_1h: number;
             last_error: string | null; series_scored: number };
    baselines: { last_fit_duration_s: number; series_fitted: number; rejected: number };
    findings: { open: number; opened_24h: number;
                by_lane: Record<FindingLane, number>; by_kind: Record<FindingKind, number> };
    incidents: { open: number };
    explain: { runs_1h: number; fallbacks_1h: number };
  };
}
/** The `facets` object on GET /findings?with_facets=true. */
export interface AnomalySummary {
  open_incidents: number; open_findings: number; open_sensor: number; new_24h: number;
  by_state: Record<FindingState, number>; by_lane: Record<FindingLane, number>;
  by_kind: Record<FindingKind, number>;
}
export interface BulkFeedbackReport {
  incident_id: string; succeeded: number;
  skipped: { finding_id: string; state: FindingState; error: string }[];
  failed: { finding_id: string; error: string }[];
}
```

Methods all go through `request()` (`api/client.ts:68`), so 401 handling is
shared. Query strings use the repeated-key builder from `alarms()`
(`:4631-4641`), extracted as a local `qs()` helper. Lists are `Page<T>`
(`:1813`), paged by `limit`/`offset` with `with_total=true`.

```ts
anomalyFindings: (p: Record<string, string | string[] | undefined>) =>   // p.with_facets = 'true'
  request<Page<AnomalyFinding> & { facets?: AnomalySummary }>(`/anomalies/findings${qs(p)}`),
anomalyFinding: (id: string) =>
  request<AnomalyFindingDetail>(`/anomalies/findings/${id}`),
findingFeedback: (id: string, body: { action: FeedbackAction; reason?: DismissReason; note?: string }) =>
  request<AnomalyFinding>(`/anomalies/findings/${id}/feedback`, { method: 'POST', body: JSON.stringify(body) }),
anomalyIncidents: (p: Record<string, string | string[] | undefined>) =>
  request<Page<AnomalyIncident>>(`/anomalies/incidents${qs(p)}`),
anomalyIncident: (id: string) =>
  request<AnomalyIncidentDetail>(`/anomalies/incidents/${id}`),
incidentFeedback: (id: string, body: { action: Exclude<FeedbackAction, 'reopen'>;
                   scope?: 'symptoms' | 'all' | 'sensor'; reason?: DismissReason; note?: string }) =>
  request<BulkFeedbackReport>(`/anomalies/incidents/${id}/feedback`, { method: 'POST', body: JSON.stringify(body) }),
explainIncident: (id: string, force = false) =>
  request<ExplainResponse>(`/anomalies/incidents/${id}/explain`, { method: 'POST', body: JSON.stringify({ force }) }),
explanationFeedback: (id: string, body: { useful: boolean; note?: string }) =>
  request<{ ok: boolean; up: number; down: number }>(`/anomalies/incidents/${id}/explain/feedback`,
    { method: 'POST', body: JSON.stringify(body) }),
anomalyDetectors: () => request<{ items: AnomalyDetector[] }>('/anomalies/detectors'),
editAnomalyDetector: (id: string, patch: { enabled?: boolean; params?: Record<string, unknown> }) =>
  request<DetectorPatchResult>(`/anomalies/detectors/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
anomalyOverrides: (p: Record<string, string | undefined> = {}) =>
  request<Page<AnomalyOverride>>(`/anomalies/overrides${qs(p)}`),
putAnomalyOverride: (detectorId: string, deviceId: string,
                     body: { enabled?: boolean; params?: Record<string, unknown>; reason?: string; expires_at?: string | null }) =>
  request<AnomalyOverride>(`/anomalies/overrides/${detectorId}/${deviceId}`, { method: 'PUT', body: JSON.stringify(body) }),
removeAnomalyOverride: (detectorId: string, deviceId: string) =>
  request<{ ok: boolean }>(`/anomalies/overrides/${detectorId}/${deviceId}`, { method: 'DELETE' }),
expectedBand: (p: { device_id: string; metric?: string | string[]; instance?: string;
                    from: string; to?: string; interval?: string; detector?: string; k?: string }) =>
  request<{ items: ExpectedBand[] }>(`/anomalies/expected${qs(p)}`),
anomalyExclusions: (p: Record<string, string | undefined> = {}) =>
  request<Page<AnomalyExclusion>>(`/anomalies/exclusions${qs(p)}`),
createAnomalyExclusion: (body: { device_id?: string; room_id?: string; datacenter_id?: string;
                                 detector_id?: string; metric_key?: string;
                                 starts_at: string; ends_at?: string | null; reason: string }) =>
  request<AnomalyExclusion>('/anomalies/exclusions', { method: 'POST', body: JSON.stringify({ ...body, source: 'manual' }) }),
deleteAnomalyExclusion: (id: string) =>
  request<{ ok: boolean }>(`/anomalies/exclusions/${id}`, { method: 'DELETE' }),
anomalyHealth: () => request<AnomalyHealth>('/anomalies/health'),
```

There is no separate summary method: the tiles read `facets` from
`anomalyFindings`, which is called with `with_facets=true`.

**Query keys**:

| Key | Holds |
|---|---|
| `['anomaly-findings', …filters]` | the findings list |
| `['anomaly-finding', id]` | one finding |
| `['anomaly-incidents', …filters]` | the incidents list |
| `['anomaly-incident', id]` | one incident |
| `['anomaly-summary', …filters]` | the `with_facets=true` findings call that feeds the tiles and facet counts |
| `['anomaly-health']` | `/health` |
| `['anomaly-detectors']` | the catalogue |
| `['anomaly-overrides']` | overrides |
| `['anomaly-exclusions']` | exclusions |
| `['device-findings', deviceId]` | the device's open findings |
| `['expected', deviceId, interval, from, to, metrics]` | bands |

A successful feedback action invalidates `anomaly-findings`, `anomaly-finding`,
`anomaly-summary`, `anomaly-incident`, `anomaly-incidents` and
`device-findings`. When the finding has an `alarm_id`, it also invalidates
`alarms` and `alarm`.

`Alarm` is **not** changed; D4 avoids the need.

---

## 17. Acceptance list

### Boundary

- [ ] `git diff --stat` touches only the files in §15. Nothing changes under
      `features/home/`, `features/assets/` (imports only), or `features/estate/`
      (the optional Thermal link aside), nor in the other six analytics views.
- [ ] A `TimeChart` snapshot without `band`, `marks` or `width` is byte-identical
      to before the change, on a curated CRAH panel and on a per-unit server
      panel.
- [ ] `/` Home renders identically with anomaly detection on and off, apart
      from counter values changing as derived alerts open.
- [ ] No hex literal appears in any new or edited `.ts`/`.tsx`, and no
      `fill="var(` appears in SVG. Checked with
      `rg "#[0-9a-fA-F]{3,8}\b" frontend/src/features/analytics/anomaly* frontend/src/features/settings/anomaly* frontend/src/features/alarms/AnomalyEvidence.tsx`.
- [ ] `anomaly.css` contains no bare `vh`.
- [ ] `scripts/validate_palette.js` passes with the new tokens.

### Triage

- [ ] The Anomalies tab is last. The other six tabs keep their order and URLs.
- [ ] The room toolbar is absent on the Anomalies tab and unchanged on the
      other six.
- [ ] The KPI band stays on the title row at 1280×800 and at 1536×864.
- [ ] Every filter, the view, the page and the selection survive a reload and a
      pasted URL.
- [ ] Paging is stable under the default impact sort: going to page 2 and back
      to page 1 shows the same rows.
- [ ] The pager is the shared `Pagination`: 25/50/100/200 rows per page, and the
      Go-to field when there are two or more pages.
- [ ] No advisory row shows `--critical` or `--major` ink.
- [ ] Viewer: no feedback buttons. Operator: Acknowledge, Confirm fault, Dismiss
      and Reopen as in §5.6. Dismiss cannot be sent without a reason.
- [ ] Alert-lane findings show Acknowledge. Pressing it acknowledges the alarm too: the alarm list shows ACKNOWLEDGED after refetch. An acknowledgement on the alarm reaches the finding within one sweep (doc 30 §4.4).
- [ ] Dismiss is disabled until a reason is picked. No `reason` is sent with any other action.
- [ ] Reopen is offered on acknowledged, confirmed and dismissed findings, and on nothing else.
- [ ] The State facet opens with open, acknowledged and confirmed pressed, matching the API default.
- [ ] Each `/expected` `reason` shows its own caption (§7.2), and a `rule_limit` band draws as a limit line with no fill.
- [ ] The detector Save dialog shows the version, the reset sentence and the superseded sentence. The L5 row's Save shows no version.
- [ ] Disabling a detector warns that its open findings are resolved and their alerts clear.
- [ ] With `enabled = false`, the off state renders with the copy for the
      viewer's role.
- [ ] A sweep older than 3 × `sweep_s` shows the stale banner.
- [ ] A burst of 30 `anomaly.finding.opened` frames triggers at most one refetch
      per 2 s, and does not reorder the list while a row has focus.

### Detail

- [ ] The sections follow the §6.1 order, and the Summary is present when the
      explainer is `none`.
- [ ] **With `explainer = none`, no element with class `anom-ai` exists in the
      DOM.**
- [ ] With an explainer enabled, the section is collapsed by default, labelled
      "AI-generated explanation", and rendered as plain text. A `rejected`
      status shows the withheld sentence and no partial text.
- [ ] Every evidence chip in the AI text focuses an existing evidence row. A chip
      for an ID that is not in the bundle never renders: the validator prevents
      it and the UI checks again.
- [ ] The chart shows the band, the dotted expected line, the finding span and
      the basis line. An L2 finding shows a reference line instead of a band.
- [ ] A band with a 3-hour excluded gap draws as two runs, and no path `d`
      contains `NaN`.
- [ ] The incident tree can be navigated by keyboard alone: Tab into it, the
      arrow keys within it, Enter to open a member.

### Device and alarm

- [ ] For a device with no findings, the Telemetry tab is unchanged: no line, no
      extra panel, and no requests beyond the findings query.
- [ ] A chiller with an open CH-2 shows an "Under watch · COP" panel with a band.
- [ ] The band mode (Findings, All modelled, Off) is held in `?band=`.
- [ ] A derived alarm shows the evidence panel in both the `/alarms/:id` pane and
      the home modal. A threshold alarm shows no panel and is identical to
      before.
- [ ] `/alarms?detection=derived` filters the list, and the select reflects the
      URL.

### Settings

- [ ] The nav entry sits after Ticketing and the route loads. Viewers see the
      page read-only.
- [ ] Every numeric parameter uses `NumberInput`: clearing a field and typing
      "8" gives "8", not "08" or the fallback.
- [ ] An out-of-range value blocks Save and shows the inline message. "Save as
      v{n}" shows the diff and the reset warning before sending.
- [ ] Overrides and exclusions page with `usePaged`. Rows from maintenance or the
      injection ledger cannot be deleted.

### Accessibility

- [ ] An axe run (the chrome-devtools a11y skill) reports no serious or critical
      issues on the triage, detail, settings and device Telemetry views, in both
      palettes.
- [ ] Triage tab order: view Seg → range → facets → selects → search → rows →
      pager → detail.
- [ ] With colour removed (greyscale emulation), kind, lane, state, severity,
      band, mark and changed parameters are all still distinguishable.

### Screenshots

Each screenshot is taken in **dark and light**, at 1536×864 (the zoom 0.9 path)
and 1280×800, unless noted otherwise. They are taken against a throwaway
backend on :8010 with `VITE_API_PROXY`, so the live stack is untouched (standing
rule). The simulator runs with `DCIM_SIM_AIR_MODEL=spatial`, with one doc 32
sensor or degradation injection active.

- [ ] `anom-incidents-{dark,light}.png`: Incidents view, one incident expanded,
      detail closed.
- [ ] `anom-findings-detail-{dark,light}.png`: Findings view with a CH-2 detail
      open and its band visible.
- [ ] `anom-sensor-{dark,light}.png`: Sensor health view.
- [ ] `anom-incident-tree-{dark,light}.png`: an incident detail showing the tree,
      "Also considered" and the context items.
- [ ] `anom-ai-open-{dark,light}.png`: explainer `cloud` or `local`, with the AI
      section expanded.
- [ ] `anom-ai-none-{dark,light}.png`: explainer `none`, same incident, no AI
      section.
- [ ] `anom-empty-{dark,light}.png` and `anom-off-{dark,light}.png`.
- [ ] `anom-dismiss-dialog-{dark,light}.png`: reason required, focus inside the
      dialog.
- [ ] `dev-telemetry-band-{dark,light}.png`: CRAH Air temperatures panel with a
      band, plus an Under watch panel for filter ΔP.
- [ ] `alarm-evidence-{dark,light}.png`: `/alarms/:id` for a CR-1 derived alert.
- [ ] `alarm-evidence-modal-{dark,light}.png`: the same alarm in the home modal.
- [ ] `set-detectors-{dark,light}.png`: one detector expanded, with a changed
      parameter and an inline error.
- [ ] `set-exclusions-{dark,light}.png`: a manual, a maintenance and an injected
      row.
- [ ] `anom-narrow-{dark,light}.png` at **390×844**: triage cards, and the detail
      replacing the list.
- [ ] `anom-greyscale.png`: Findings view under CSS `filter: grayscale(1)`. One
      palette is enough here.

---

## 18. Reconciliation status and dependencies

### Settled by doc 30 (§15)

| Item | Settlement |
|---|---|
| Topic and event names | Topic `anomalies`, dotted names kept |
| D4–D11 | As listed in §3 |
| Version bump on `PATCH` | Resets accumulators and may resolve findings as `superseded`. §9.4 carries the copy |
| Facets | Come from `with_facets` |
| Paging | `limit` / `offset` / `total` |
| Alarm types | Snake_case per doc 29 §9.3. Severity is capped at MINOR, and MAJOR only comes from L5 escalation behind `l5_major_enabled` |

### Open, owned elsewhere

- **Doc 33 task 5.5** places `AiExplanation` in `AnomalyEvidence.tsx`. This doc
  recommends a link only (§8.2). That is a contract deviation for doc 33 to
  accept or reject.

### Dependencies on doc 33's open questions

**Q15 — no frontend test runner.**

- `frontend/package.json` has no test runner (doc 33 §2, risk R25). The
  component tests in §15 therefore **depend on Q15**.
- If vitest is added, those tests ship with the task that adds the component.
- If it is not, three checks stand in for them:
  - `tsc --noEmit` and the CI build;
  - the live acceptance list in §17, checked by hand through the
    chrome-devtools harness;
  - a scripted DOM check of the byte-identical `TimeChart` render, which
    compares the SVG markup of a device page before and after.
- Either way, the "no `NaN` in a band path" and "no `.anom-ai` when the
  explainer is `none`" items in §17 stay as manual checks.

**Q16 — sensor findings have no screen until Phase 3.**

- In the current plan, L0 sensor-health findings exist from Phase 1 but are
  visible only through the API, `/health` and the settings counts (doc 33
  D18).
- **If the user chooses the Phase 1 screen** (about 10 h), ship a **minimal
  sensor-health list** in Phase 1:
  - What it is: the §5.4 Sensor health view alone, as
    `/analytics?view=anomaly&av=sensor`, with no Incidents or Findings cells,
    no KPI band and no detail pane.
  - Rows link to the device Telemetry tab.
  - Feedback is per row: Acknowledge, plus Dismiss with the reason picker.
  - Paging uses `Pagination`, and the `anomalies` topic keeps it live.
  - Phase 3 then grows this into the full triage tab without moving its URL.
- **If the user declines**, Phase 1 adds one line to the settings health
  line: "{n} open sensor findings — visible in triage from Phase 3".
  - The number comes from `/health.detail.findings.by_kind.sensor`.
  - The line tells the reader the findings exist.
  - It gives no link to a screen that is not there yet.

### Phase dependencies for the UI (doc 33 task numbers)

| Task | Phase | UI scope | Depends on |
|---|---|---|---|
| 1.16 | 1 | Alarm evidence panel with in-place "Show all evidence" and the `rule_limit` band chart; `?detection=` select | `alarm_id` filter, `/expected` `rule_limit` |
| 1.17 | 1 | `NumberInput` port; settings: detectors, L5 row, overrides, exclusions, health lines | `/detectors`, `/health` |
| (Q16) | 1 | Optional minimal sensor-health list | Q16 answer |
| 2.7 | 2 | Incident tree in the alarm evidence panel; incident bulk actions once `POST /incidents/{id}/feedback` ships | incidents API |
| 3.10 | 3 | Device findings line, band mode, Under watch panels | fitted baselines |
| 3.11 | 3 | Full triage tab and `AnomalyDetail` | — |
| 4.8 | 4 | Zone tier (`zone` in place of `room`) and the sensor-health count | influence fit |
| 5.5 | 5 | AI section, thumbs | explainer configured |
