# 18. Alert taxonomy

Status: phases **1-4 built** (classifier and schema, boolean rules seeded
disabled, roll-up and API, UI). Phase 5 (tests) is partly done - the
exhaustiveness, disjointness and roll-up suites exist; the golden backfill set
does not. Phase 6 (capacity rules) remains: the category, the counter and the
column all exist and read zero.

The five-bucket vocabulary - thermal / connectivity / datapoint / anomaly /
other - was served alongside the eight through phase 3 so the API could move
ahead of the UI, and was **removed with the phase 4 frontend**:
`core/alarm_categories.py`, the legacy roll-up columns, the `legacy` branch of
the drill-down and the old flat keys on every alert block are gone. The retired
names now 400 rather than resolving, deliberately: a bookmarked drill-down from
the old UI must fail loudly instead of opening an empty modal that reads as
"nothing wrong in this category".

## 1. Why this changes

The five categories we shipped (connectivity / thermal / alarms / datapoint /
anomaly) mix three different questions, and the estate showed the cost.

**They mix axes.** "Thermal" is a fault domain. "Faults from BACnet and Modbus"
is a *transport*. "Analytics" is a *detection method*. A taxonomy used to route
work has to answer one question - what kind of thing is wrong, and therefore
who acts - or the counters cannot be read.

The concrete failures:

* A CRAH fan failure arriving as a BACnet alarm point and the same failure
  inferred from `fan_speed = 0` over SNMP land in different categories. Same
  fault, two buckets, two owners.
* "Datapoint" means *threshold breach* in our scheme and *missing data* in the
  BMS tooling operators already use. We then had nowhere to file "device
  answers, telemetry stopped" - the failure where every dashboard stays green
  while the numbers age. We detect it (`telemetry_stale`) and could not
  classify it.
* Power has no category at all, though it is the largest single cause of
  outages in the industry's own numbers. UPS-on-battery lands under "alarms" or
  "datapoint" depending on how it was detected.
* Redundancy and headroom - the things a DCIM knows and a generic monitoring
  stack does not - have no home, so they default into "analytics".

**And a gap the categories hid:** 38 equipment alarm points stream in from
BACnet and Modbus onto the boolean metric `alarm_state`, and **none of them can
raise an alarm**, because the engine only compares floats and no rule covers
`alarm_state`. Three are asserted right now on the energy monitors
(`Alarm_Undervoltage`, `Alarm_SensorFault`, `Alarm_UnderFrequency`). The
category the old scheme called "Alarms" had no implementation behind it.

## 2. The taxonomy

One axis: **the failing thing's domain, which is the same as who owns the first
five minutes.**

| Category | Means | Owner |
|---|---|---|
| `visibility` | We cannot see it: unreachable endpoint, stale telemetry, missing datapoint, collector or ingest degraded, auth failure | monitoring |
| `environmental` | The space: room and intake air, humidity, dew point, leak, airflow, containment | facilities |
| `cooling` | Cooling equipment and loops: CRAH/CRAC, chiller, CDU, tower, pump, valve, staging, plant capacity | plant |
| `power` | The electrical chain: utility, ATS, generator, UPS, switchgear, PDU/RPP, branch circuits, phase balance | electrical |
| `it_equipment` | The host: CPU, memory, disk, fan, PSU, component temperature, predicted hardware failure | IT |
| `network` | Fabric and transport: link and interface state, errors, path redundancy, adjacency | network |
| `capacity` | Headroom and resilience: redundancy lost, single-corded load, days of supply, efficiency excursion | planning |
| `uncategorised` | Anything unmatched, kept visible on purpose | triage |

`uncategorised` is not a failure of the design; it is the instrument that
measures the design. A point nobody classified must be countable, not filed
into whichever bucket is nearest.

## 3. What is an attribute, not a category

Everything the old scheme smuggled into the category axis becomes a field:

| Field | Values | Replaces |
|---|---|---|
| `severity` | CRITICAL / MAJOR / MINOR / WARNING | - |
| `detection` | `threshold` · `state` · `absence` · `derived` · `forecast` | the "Analytics" category |
| `source_protocol` | snmp / bacnet / modbus / redfish / gnmi | the "faults from BACnet/Modbus" definition |
| `is_symptom` | root vs downstream | already exists |
| `scope` | device / rack / room / site / plane | - |

An anomaly in chiller COP is **cooling, detection=derived**. A predicted disk
failure is **it_equipment, detection=forecast**. The analytics view becomes a
filter that works across every category, instead of a bucket that grows every
time we add a detector - and a condition no longer changes category when its
detection improves.

## 4. Classification, in resolution order

`core/alarm_categories.py` becomes declarative. Three layers, first match wins:

1. **`alarm_type` -> category.** Explicit, ~30 entries, covers platform alarms
   and named conditions.
2. **`(device_type.category, metric_key)` -> category.** The role-sensitive
   layer. `fan_speed` on a `cooling` device is cooling; on an `it` device it is
   it_equipment. `power_draw` on a `power` device is power; on a server it is
   it_equipment.
3. **Metric group default** (`registry.yaml`'s `group`), then `uncategorised`.

The SQL `CASE` stays generated from the same tables, extended to carry the
device-type join, so the roll-up query and `categorise()` cannot drift.

## 5. Mapping the plane we actually have

Counts are the live inventory, not estimates.

| Category | Metrics | Equipment points and traps | Device roles |
|---|---|---|---|
| visibility | `reachable`, `poll_latency`, plus the *absence* of any metric | `endpoint_unreachable`, `telemetry_stale`, `collector_*`, `ingest_*` | any |
| environmental | `ambient_temperature`, `relative_humidity`, `dew_point`, `airflow`, `inlet_temperature` on sensors | 14 sensor traps, `Alarm_Leak` | `environment` |
| cooling | 26 `cooling` metrics, `exhaust_temperature` | 16 points across chiller, pump, tower, crah, cdu, valve | `cooling` |
| power | 26 `power` metrics on power-role devices | 55 traps (UPS 13, PDU 18, generator 12, ATS 8, switchgear 4) + 18 points on ups, gen, ats, energy_monitor | `power` |
| it_equipment | `cpu_*`, `memory_*`, `disk_*`, `fan_speed*`, `component_temperature`, `psu_*`, `power_draw` on IT | 8 enterprise traps, server power on/off | `it` |
| network | 12 `interfaces` metrics | `linkDown`, `linkFlap`, `bgp*`, `authenticationFailure` | `network` |
| capacity | derived from `load_pct` vs rating, `design_it_kw`, redundancy | none yet - see phase 6 | site / room / rack |

`inlet_temperature` is the deliberate split: on a rack sensor it is a space
condition judged against the room envelope (**environmental**); on a server it
is one host's intake (**it_equipment**). Same rule that separates a CRAH fan
from a server fan.

## 6. Phases

### Phase 1 - classifier and schema

* Rewrite `core/alarm_categories.py` as the three-layer registry above.
* Migration `0019`: `alarm.category`, `alarm.detection`, indexed, written at
  raise time rather than recomputed per query; `alarm_rule.category` (override)
  and `alarm_rule.detection`.
* Backfill existing alarms through the classifier. Anything unmatched stays
  `uncategorised` rather than being guessed.
* No behaviour change. Counts move; nothing new fires.

### Phase 2 - boolean rules

The engine compares floats only. Add:

* `alarm_rule.metric_kind` = `numeric` | `boolean`, and `raise_on` = true/false
  (an `alarm_state` of true is a fault; an `equipment_state` of false on a unit
  that should be running is also a fault).
* Dwell and hysteresis reuse the existing sample counters - no second state
  machine.
* Seed by point family, not per point: one rule for `alarm_state`, dwell 2,
  severity by class (below).

**Severity classes for the 38 points:**

| Severity | Points |
|---|---|
| MAJOR | `Alarm_Leak`, `Alarm_LowFlow`, `Alarm_FlowLoss`, `Alarm_PhaseLoss`, `Alarm_Overcurrent`, `Alarm_Undervoltage`, `Alarm_UnderFrequency`, `Battery_Fault`, `Charger_Fault`, `Rectifier_Fault`, `Phase_Fault`, `Low_Battery`, `Fail_To_Transfer`, `Alarm_Fault`, `Alarm_PumpFault`, `Alarm_ActuatorFault`, `Alarm_HighPressure`, `Alarm_HighTemp`, `Alarm_HighSupplyTemp`, `Alarm_AirflowLoss` |
| WARNING | `Filter_Dirty`, `Alarm_HighVibration`, `Alarm_LowBasin`, `Alarm_SensorFault`, `Alarm_HighTHD`, `Alarm_VoltageImbalance`, `Alarm_LowEvapTemp`, `Alarm_CondPressLimit`, `Alarm_HighCHWSupply`, `Alarm_HighReturnAir`, `Not_In_Auto`, `Alarm_Low_Fuel`, `Alarm_Low_Coolant`, `Alarm_Transfer`, `Fan_Fault` |

Ship the rules **disabled**. Verify classification against live data first,
then enable and watch the raise rate - the alarm-management convention is that
a sustained rate above roughly ten alarms per ten minutes is a flood, and the
three asserted points mean this will not start at zero.

### Phase 3 - roll-up and API

* `repositories/sites.py` and `repositories/estate.py` move to the eight
  categories through the generated CASE.
* `/estate/alerts?category=` accepts the new values; the five old values are
  accepted for one release and mapped, so a deploy cannot break the UI midway.
* Site and room rows gain a `by_detection` breakdown so "only what analytics
  found" is a filter rather than a category.

### Phase 4 - UI  *(built)*

* **Strip: five grouped counters** - Power · Cooling & Environment · IT &
  Network · Visibility · Capacity, with the all-categories total between them.
  Uncategorised appears as a sixth only when non-zero.
* **Table: eight indicator columns**, one per category, plus ALM. Nothing is
  hidden by the grouping; the strip is the headline, the table is the detail.
  A cell with a count is also the way into the rooms behind it.
* Drill-down modal carries `severity` and `detection` facets, folded from the
  rows it shows rather than fetched, so the two cannot describe different
  instants. A grouped counter runs one query per category and keeps the rows
  per room AND per category - merging them would need a rule for `devices`,
  where one device can carry alerts in two categories.
* The legend is generated from `/estate/alert-categories`, which is generated
  from the classifier - including the example alarm types, which are real
  entries out of `BY_ALARM_TYPE` rather than illustrations of it.
* **Colour is the strip group, not the category.** Five hues for eight
  categories: the two categories that share an owner share a hue and are told
  apart by their glyph. Eight hues would be eight things to learn and several
  of them indistinguishable on a wall display.

### Phase 5 - tests

* **Exhaustiveness.** Every one of the 106 trap types and 38 alarm points maps
  to a category or appears in an explicit `KNOWN_UNMAPPED` set. Adding a point
  to the simulator that nobody classified fails the suite.
* **Disjointness.** No `alarm_type` resolves to two categories.
* **Role sensitivity.** `fan_speed` crah -> cooling, server -> it_equipment;
  `power_draw` pdu -> power, server -> it_equipment; `inlet_temperature`
  sensor -> environmental, server -> it_equipment.
* **Backfill.** A golden set of historical alarms with expected categories.
* **Boolean rules.** Raise, clear, dwell, and `raise_on=false`.

### Phase 6 - capacity rules (next pass)

Category, counter and column exist from phase 1 and read zero. The detection -
redundancy lost (N+1 / 2N), single-corded load, headroom against
`design_it_kw`, days of supply - lands later and needs no schema or UI change
when it does.

## 7. Decisions taken (2026-08-24)

| Decision | Choice | Why |
|---|---|---|
| Strip layout | Five grouped counters, eight table columns | The strip is the wall-display headline; grouping keeps the numbers legible and matches the reference layout, and the table loses nothing |
| Equipment alarm severity | Split by point class | Integrity faults that threaten load now are MAJOR; wear and hygiene are WARNING. Rationalising by consequence is the whole point of a severity axis |
| Capacity | Classify now, rules next pass | Keeps this pass about categorisation; the slot exists so adding rules is not a migration |
| `inlet_temperature` | By device role | 618 servers would otherwise dominate a counter facilities read as "the room" |

## 8. Out of scope

Alert routing and notification, per-room threshold configuration UI, alarm
suppression policies, and correlation changes. This document is about which
category a condition belongs to and what fires it - nothing else.
