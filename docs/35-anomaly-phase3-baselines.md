# 35 — Anomaly detection Phase 3: learned baselines (design for review)

Status: **proposal, not built.** Revises the Phase 3 section of
`33-anomaly-implementation-plan.md` in the light of what changed since it was
written (2026-11-03).

---

## 1. Why, in one paragraph

Today's detectors (Phase 0–1) are fixed engineering rules: supply against
setpoint, command against proven flow, a rack's inlet against the room's p90.
That is how BMS fault detection is done in practice (ASHRAE Guideline 36,
NIST APAR) and it explains itself well. What it cannot do is notice that a
device is behaving unlike **itself** — "this rack usually draws 8 kW and now
draws 12 kW with no more work", "UPS load fell from 65 % to 25 %", "battery
temperature is creeping up". That needs a learned picture of normal per
device: a baseline. Phase 3 adds it.

## 2. What changed since doc 33 was written

| Change | Effect on Phase 3 |
|---|---|
| Findings no longer raise alarms by default (migration 0124, platform 4a8bdfe). They show in the 3D device panel and as a marker on the unit | Phase 3 findings are **panel-only from day one**. No new alarm types, so doc 33 task 3.5 (taxonomy entries for promotions) is dropped |
| Migration numbers moved on (0114 was taken by other work; head is 0124) | Phase 3's migration is **0125** |
| Your priorities are power, UPS, thermal and server behaviour, not only the cooling plant | Phase 3 splits: **3a** estate baselines (power, UPS, thermal, server, network) first; **3b** the cooling-plant residuals of doc 33 after |
| The simulator changed regime several times in the last 30 days (compute re-SKU 2026-10-29, outlet model, per-outlet current) | A fit must not learn across a regime change. The fit window starts after the last one, or the change is an exclusion (§6) |
| The stack runs about 7 hours a weekday, so the history has gaps every night and weekend (§3) | Guards count hours of data, not days; profiles pool by hour of day; regression is preferred |

## 3. The history is part-time, and that shapes the design

The first draft of this section said the 1 h and 1 d rollups had stalled.
They had not. Checked layer by layer on 2026-11-03, there is **no raw data at
all** for the hours the rollups were missing: the stack runs only while it is
being worked on.

| Day | Hours with data |
|---|---|
| 2026-10-28, 10-29, 10-30 | 7 each |
| 2026-10-31, 11-01 (weekend) | 0 |
| 2026-11-02 | 6 |
| 2026-11-02 15:00 → 11-03 07:00 (overnight) | 0 |

The rollups follow their policies exactly (1 h trails by 1–2 h, 1 d by a day
plus the bucket). What is unusual is the history itself, and it changes three
things:

- **"14 days of history" means hours, not calendar days.** At about 7 hours
  a weekday, 14 days' worth (336 h) takes about 10 weeks. The guard is
  expressed as **at least 120 hours of clean data spread over at least 10
  separate days**.
- **Hour-of-week profiles cannot be learned** — most of the 168 slots never
  see data. Profiles pool by **hour of day** instead, and only the hours the
  estate has been seen in are scored; an hour with fewer than 8 past samples
  is "learning".
- **Every morning is a cold start**: collectors reconnect, stale readings
  clear, the simulator's walks resume. The first 30 minutes after a gap of
  more than 1 h are excluded from fits and from scoring, as a restart is.

This favours the **regression** shape (§4): "power given CPU load" does not
care what time it is, so it learns from part-time data as well as from
continuous data. The alternative is to leave the stack running around the
clock for the learning period — a decision for §9.

## 4. What a baseline is here

Two model shapes cover every Phase 3a detector. Both are small, explainable
and fitted nightly from `telemetry_1h`.

| Shape | What it learns | Used for | Explains itself as |
|---|---|---|---|
| **Profile** | Hour-of-day median and robust spread (MAD) of one series over the hours it has been seen (hour-of-week once the history is continuous) | Things with a daily/weekly rhythm and no driver we can measure: UPS load, phase imbalance, interface error rate | "Tuesdays at 14:00 this UPS runs 61–68 %; it is at 25 %" |
| **Regression (OLS)** | One series as a function of its driver(s) | Things that should follow a cause: PDU power ~ the CPU load of the servers it feeds; battery temperature ~ room ambient; server fan ~ inlet and CPU; UPS runtime ~ load | "With this much server load, this PDU should draw 7.6–8.4 kW; it draws 12.1 kW" |

**Detection** is the same for both: residual = (observed − expected) / σ.
A fast fault trips an **EWMA** of the residual above a threshold for a dwell;
a slow drift trips a two-sided **CUSUM**, so a creep of 0.1 σ an hour is found
within a bounded number of hours. CUSUM resets on a known step (a setpoint
change, a maintenance window ending).

**Why regression first where a driver exists:** a profile alone flags every
workload change as an anomaly. Your own example says "even though its IT
workload has not increased" — that clause is the regression. Without it, a
batch job starting at 02:00 is a finding every night.

**Not a generic "3σ on every metric".** Running a profile on all ~7,000 series
and flagging anything past 3σ produces, by the arithmetic of multiple
comparisons alone, dozens of false findings a day. Each detector is named,
gated (only while the unit is running / powered) and carries a diagnosis, as
the Phase 1 rules do.

## 5. Phase 3a detectors (estate baselines)

| ID | Device | Model | Catches | Sim can test it? |
|---|---|---|---|---|
| PD-1 | PDU, energy monitor | OLS: power ~ Σ CPU utilisation of the servers it feeds (from the power chain) | Your 8 → 12 kW example: power without workload (failing PSU, misconfigured BIOS power cap, unrecorded hardware) | Partly: needs a fault kind that raises power without CPU |
| PD-2 | PDU, panel, switchgear | Profile: phase imbalance | A phase drifting out of balance | Yes, with an imbalance kind |
| UP-2 | UPS | OLS: battery runtime ~ load | Battery capacity fading (runtime falling at the same load) | Needs `battery_aging` |
| UP-3 *(new)* | UPS | Profile: load % | Load step with no planned change (your 65 % → 25 % example: a lost downstream feed or a transfer) | Yes |
| UP-1 | UPS | OLS: battery temperature ~ room ambient | Battery heating beyond what the room explains | Needs `room_hvac_loss` |
| SV-2 | Server | OLS: fan speed ~ inlet, CPU | Fan wear, a blocked intake | Needs `server_fan_wear` |
| SV-1 | Server | OLS: CPU temperature ~ CPU %, inlet | Heatsink or thermal paste failure | Needs `server_thermal` |
| SV-3 *(new)* | Server | Profile: CPU utilisation | Your 95 % vs usual 20–40 % example | Yes |
| NW-1 *(new)* | Switch, router | Profile: interface error **rate** | Error rate rising from its own baseline (cabling, optics) | Needs an error-injection kind |
| TH-1 *(new)* | Rack inlet probe | OLS: inlet ~ the CRAHs that feed it (supply temp) | A rack warming more than its cooling explains | Yes, with a local airflow fault |

`Sim can test it?` is the honest constraint. A learned detector is only
proven when the simulator can produce the fault it is meant to catch. Where it
cannot, Phase 3a still ships the detector but **seeded off**, and the only
thing it can be checked for is staying silent on clean data. The simulator
kinds needed (doc 32 §13.6, "S4") are listed in §9.

## 6. Guards (what keeps it from learning the wrong normal)

- **Minimum history.** Fewer than 120 clean hours over at least 10 days:
  the series is "learning", no finding, and the panel says so.
- **Thin buckets refused.** An hour with under half its expected samples does
  not enter the fit.
- **Exclusions.** Maintenance windows, campaign ledger windows, and regime
  changes (a simulator regeneration, a topology change, a device re-SKU)
  are cut out of the fit window.
- **Freeze while open.** A series with an open finding is not refitted —
  otherwise the baseline learns the fault and the finding resolves itself.
- **Change cap.** A coefficient moves at most 10 % a night, so one strange
  day cannot rewrite normal.
- **Budget.** At most 30 Phase 3 findings a day estate-wide and 10 per
  detector; a detector over its cap for two days is turned off with a note
  on the settings page. Chatter (open, close, open) collapses into one.

## 7. What the operator sees

All panel-only, as decided on 2026-11-03.

- **3D device panel**, the existing "Anomaly detection" section: a baseline
  finding reads like a rule finding, plus the expected band —
  "12.1 kW against an expected 7.6–8.4 kW for this server load, since 03:10".
- **Device page charts**: the expected band drawn behind the line for any
  metric with a baseline (doc 33 tasks 3.9–3.10).
- **Learning state** shown honestly: "Learning — 64 of 120 hours" instead of
  "OK" while a device has no baseline.
- **Settings > Anomaly detectors**: the new detectors, their on/off, their
  sensitivity, and how many series each has a fitted baseline for.
- The triage queue (doc 33 task 3.11) is **deferred**: with findings on the
  device, a separate queue page is less important than it was when they were
  alarms.

## 8. Build order and effort

| Step | Work | Effort |
|---|---|---|
| 0 | ~~Fix the stalled rollups~~: not a fault, the gaps are when the stack was off (§3) | done |
| 1 | Migration 0125: Phase 3a detector rows (seeded off where the sim has no truth) | 2 h |
| 2 | Nightly fit (`analytics/baselines.py`): profile + OLS, guards in §6, batched under the existing lock | 16 h |
| 3 | Residual framework (`detectors/residual.py`): EWMA, CUSUM, checkpoint/restore | 10 h |
| 4 | The ten Phase 3a detectors + their tests (fires on its synthetic fault, silent on the clean twin) | 20 h |
| 5 | `GET /anomalies/expected` (band for a device + metric over a window) | 5 h |
| 6 | Panel: band text and learning state; chart band on the device page | 10 h |
| 7 | Budget (§6) | 4 h |
| 8 | Live soak: two nights of fits inspected, then 7 days clean | wall-clock |

**About 70–80 hours for 3a**, against doc 33's ~145 h for the full Phase 3.
3b (the cooling-plant residuals CH-2/3, CT-1/4, PU-1/2/4, CR-3/4, CD-2/3/4)
reuses steps 2–3 and is about 35 h more.

**Calendar:** the first useful fit needs 120 clean hours after the last
regime change. At the current ~7 hours a weekday that is about **4 weeks**
(early December); with the stack left running around the clock, **5 days**.
The code can be built and tested before then against synthetic series.

## 9. Decisions for you

1. **Order: 3a (power, UPS, server, thermal, network) before 3b (cooling
   plant)?** Recommended: yes — it matches your examples, and the plant
   already has the most Phase 1 rule coverage.
2. **Seed the detectors the simulator cannot test as off?** Recommended: yes.
   Turning on an unproven detector is how an advisory queue earns a
   reputation for noise.
3. **Simulator fault kinds (S4) for ground truth.** Without them, half of
   §5 can only be shown to stay quiet. Recommended first four, by value:
   `battery_aging` (UP-2), `server_fan_wear` (SV-2), `room_hvac_loss` (UP-1),
   an interface-error kind (NW-1). That is simulator work (~15–20 h) on top of
   the 70–80 h.
4. **Freeze the simulator's model for the learning period** once Phase 3a is deployed, so
   the first fits learn one regime. Any regeneration or re-SKU in that window
   restarts the clock for the affected devices.
5. **Run the stack around the clock during the learning period?**
   Recommended if the host can hold it: 5 days instead of about 4 weeks, and
   real night-time and weekend behaviour to learn. The host is memory-tight
   (WSL ~640 MB free), so this is your call. Otherwise the part-time design
   in §3 works, more slowly.
