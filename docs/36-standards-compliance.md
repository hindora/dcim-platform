# DCIM Standards Compliance Assessment

5 November 2026. Living copy in Claude Docs: https://claude.ai/artifact/NC1bTm71XVagrqHxG3CzuT

## Summary

The DCIM platform meets ASHRAE TC 9.9 in full, meets ISA-18.2 / IEC 62682 in alarm behaviour but not in two of its required alarm attributes, and can support an EN 50600 class 3 facility once leak detection is monitored. Three gaps remain, all bounded: a priority matrix for alarm severities, alarm performance KPIs, and water leak sensors.

| Standard | Status | What is met | What is missing |
| --- | --- | --- | --- |
| ISA-18.2 / IEC 62682 (EEMUA 191 benchmarks) | Partial | Alarm vs alert split, designed suppression with a common alarm, flood handling, dead-bands and dwells, a full per-alarm record | Priority rationalised from consequence and time to respond; alarm-rate, flood, stale and chattering KPIs |
| ASHRAE TC 9.9 (2021 thermal guidelines) | Compliant | Inlet 18–27 °C recommended and 32 °C A1 allowable, 20 K/h rate of change, dew point −9 to 15 °C recommended and 17 °C allowable, per-room class A1–A4 | Humidity coverage: only the 18 room and rack probes report RH, so PDU-mounted probes carry no dew point |
| EN 50600-2-x (design, monitoring by availability class) | Partial | Power chain end to end, environmental control including chilled water and liquid loops, fire detection and suppression, capacity, energy and PUE, incidents and planned work | Water leak detection (0 sensors); access-control events and CCTV state |

## Scope and method

Assessed: the DCIM platform as deployed on 5 November 2026 (platform commit 104d3d0, simulator commit c5d16a3), monitoring a two-site simulated estate of 716 devices with 1,458 polled endpoints. Not assessed: the physical facility, its procedures or staffing, which the standards also cover.

Editions used: ANSI/ISA-18.2-2016 with IEC 62682:2022, and the EEMUA 191 (3rd edition) benchmark figures; ASHRAE TC 9.9 Thermal Guidelines for Data Processing Environments, 5th edition (2021); EN 50600-2-2, 2-3 and 2-5 with EN 50600-3-1 for management.

Evidence was taken from the running system, not from design documents: the alarm rule table and the classifier source, 30 days of alarm history, the metrics each device type delivered in the last 30 minutes, the sensor inventory, and the results of three injected cooling-failure campaigns (one hall losing its CRAHs; a site losing all chillers, twice). Where a standard describes a facility obligation that software can only support, the status says so rather than claiming compliance.

## ISA-18.2 / IEC 62682, with EEMUA 191 benchmarks

The platform's alarm behaviour follows the standard; its alarm attributes do not yet. Every requirement about what an alarm is, how it is suppressed and how a flood is handled is met. Two attributes the standard makes mandatory for each alarm are absent: a rationalised priority, and the performance metrics that show whether the operator can keep up.

| Clause | Requirement | Status | Evidence on the platform |
| --- | --- | --- | --- |
| 3.1 definition | An alarm requires an operator response; a condition that does not is not an alarm | Compliant | Every condition carries a response class: `alarm` (needs a response, expects acknowledgement) or `alert` (informational). WARNING and INFO conditions are alerts |
| 10.3 shelving | Operator-initiated, time-limited suppression, visible and recorded | Compliant | Maintenance windows shelve a device's alarms for the window's span; shelved alarms stay in the record with the window named |
| 10.4 designed suppression | Automatic suppression by plant state, reversible when the state ends, never silent | Compliant | The cooling chain fold: heat alarms downstream of a stopped CRAH, CDU or chiller plant fold under that cause, one line per room and kind stays visible, all are released 10 min after the cause clears; each fold and release is a line in the alarm's record |
| 10.4 / EEMUA flood | More than 10 alarms in 10 min is a flood; expected consequences fold under one common alarm with the post-trip action | Compliant | A site losing its chiller plant raised 727 alarms and showed 9 console lines plus one `cascade_incident` common alarm at the worst member's severity. Target: at most 10 lines in the first 10 min |
| 6.3 dead-band and on/off delay | Hysteresis and dwell on every threshold alarm against chattering | Compliant | Every rule has a clear threshold below its raise threshold and a dwell in samples (inlet high: 27 raise, 25 clear, 2 samples) |
| 6.2.3 priority | Priority set from consequence severity and time to respond through a documented matrix; EEMUA target about 80 % low, 15 % medium, 5 % high | Not compliant | Severities come from each rule's threshold bands and from vendor trap severities. No matrix exists and the distribution is not measured |
| 6.2 rationalisation record | Each alarm documented with cause, consequence, corrective action and allowable response time | Partial | Rules carry a description and limits; incidents and anomaly findings carry a cause. No per-alarm action or response time is recorded |
| 16 monitoring and assessment | Alarm rate per operator position, peak rate, time in flood, stale alarms, chattering alarms, most frequent alarms | Not compliant | Not measured. The platform health page reports pipeline state and alarm episodes, not operator load |
| 5 philosophy document | A written alarm philosophy the system is built to | Partial | The alert taxonomy (docs/18) and the anomaly design (docs/28) state the ownership, class and suppression rules; priority and KPI policy are not written |
| 11 audit | Every alarm action attributable: who acknowledged, cleared, shelved, with notes | Compliant | Acknowledge with note, clear by hand, shelve and release are recorded with actor and time, and shown in the alarm's timeline |

EEMUA 191 benchmarks for reference: an operator can manage about 1 alarm per 10 minutes on average (acceptable) and 2 per 10 minutes at most (manageable); more than 10 in 10 minutes is a flood; no more than 5 % of alarms should be high priority.

## ASHRAE TC 9.9 thermal guidelines

The platform alarms on all four sides of the recommended envelope and on the A1 allowable limits, and judges each room against its declared class. Coverage, not rules, is the remaining limit: a dew point can only be derived where a probe reports both temperature and humidity.

| Guideline (5th edition, 2021) | ASHRAE value | Platform rule | Status |
| --- | --- | --- | --- |
| Recommended inlet temperature, upper | 27 °C | `inlet_temp_high` > 27 (clear 25), WARNING; room probes `ambient_temp_high` > 27 | Compliant |
| Allowable inlet temperature, class A1 | 32 °C | `inlet_temp_critical` > 32 (clear 30), CRITICAL | Compliant |
| Recommended inlet temperature, lower | 18 °C | `inlet_temp_low` < 18 (clear 19), WARNING; room probes `ambient_temp_low` | Compliant |
| Rate of change | 20 K/h (5 K in 15 min) | RM-2 inlet rate of change, 20 K/h over 15 min, advisory finding | Compliant |
| Recommended dew point, upper | 15 °C | `dew_point_high` > 15 (clear 14), WARNING | Compliant |
| Allowable dew point, class A1 | 17 °C | `dew_point_critical` > 17 (clear 16), CRITICAL | Compliant |
| Recommended dew point, lower | −9 °C | `dew_point_low` < −9 (clear −7), WARNING | Partial |

Dew point is derived in the ingest path (Magnus formula, within 0.1 K) wherever a device reports temperature and relative humidity in the same poll; it is never built from half a pair, and a humidity of zero is treated as a missing hygrometer. The low dew point rule is marked partial because the A1 allowable lower limit (−12 °C) has no rule of its own, and because PDU-mounted probes in this estate report temperature only, so 18 of the 82 environmental probes carry a dew point today.

Room grading uses the room's declared class (A1–A4) on the thermal page: each rack's intake is placed inside or outside the recommended and allowable bands for that class, and a hall where half the racks exceed the recommended maximum is reported as a room-wide event rather than a hot spot.

## EN 50600-2-x monitoring coverage

The platform monitors every power and cooling point an availability class 3 design calls for, and the fire system. Two areas are short: water leak detection has no sensors at all, and physical security stops at door contacts. EN 50600 certifies the facility, so these are coverage gaps the platform can close only when the sensors exist.

| Part and area | Points the standard expects monitored | Monitored today | Status |
| --- | --- | --- | --- |
| 2-2 Power distribution | Utility supply, generator, switchgear, transfer switches, UPS, distribution, rack PDUs and circuits | Utility feed (V, A, kW, PF, THD), generator (fuel, coolant, load, run state), switchgear and MCC/MPP buses, ATS position and transfer alarms, UPS (load, battery runtime and health, bypass, on-battery), RPP and PDU per phase, bank breaker state, per-outlet current | Compliant |
| 2-3 Environmental control | Hall temperature and humidity, cooling units, chilled-water plant, liquid cooling, leak detection | Room and rack probes (T, RH, derived dew point), CRAH supply/return/setpoint/valve/fan/filter, chillers, pumps, towers, CDU loops (supply, return, flow, approach, filter ΔP); **no water leak detection** | Partial |
| 2-1 / NFPA 72 Fire | Detection stages, panel alarm, trouble and supervisory, suppression agent and release | Smoke obscuration and stage, panel alarm/trouble/supervisory, agent pressure, release countdown and state, CRAH interlock | Compliant |
| 2-5 Physical security | Access control, door state, surveillance | 10 door contacts with door-held-open alarms; no access-control events, no CCTV state | Partial |
| 3-1 Management: capacity and energy | Power and space capacity, energy, PUE, trend | Rack, room and site power capacity and headroom, U-space, energy, PUE on the plant page, 7-day p95 | Compliant |
| 3-1 Management: operations records | Incidents, changes, alarm history, audit | Incidents with cause and members, maintenance windows as the change record, full alarm timeline, audit of every operator action | Compliant |

Availability classes: with the leak-detection gap closed, the monitoring side supports class 3 (concurrently maintainable). Class 4 adds redundancy of the monitoring path itself (dual collectors per endpoint), which the collector fleet already provides for polling but not for the database.

## Gaps and recommendations

Four items close every gap above; the first three are software, the fourth needs sensors. In the order that returns the most per day of work:

| # | Gap | Standard | Recommendation | Effort |
| --- | --- | --- | --- | --- |
| 1 | Water leak detection not monitored | EN 50600-2-3 | Add leak-rope and spot sensors under raised floors, at CRAH drains and CDU manifolds in the simulator; map them to the existing `water_leak` alarm type (Environmental, CRITICAL); show them on the floor plan | 2 days |
| 2 | Priority not rationalised | ISA-18.2 §6.2.3 | A severity matrix in Settings: consequence (safety, availability, equipment, efficiency) × time to respond (minutes, hour, shift, day) → severity, applied to the rule set and to vendor trap severities; report the resulting distribution against the 80/15/5 target | 3 days |
| 3 | No alarm performance metrics | ISA-18.2 §16, EEMUA 191 | A panel on the platform health page: alarms per 10 min per site (average and peak), minutes in flood, stale alarms (open over 24 h, unacknowledged), chattering points (raised and cleared more than 5 times a day), the ten most frequent alarm types; alert when the average exceeds 2 per 10 min | 3 days |
| 4 | Dew point only on room and rack probes | ASHRAE TC 9.9 | Have PDU-mounted probes report relative humidity (the simulator's PDU model exposes it; the SNMP mapping does not read it) so every probe carries a dew point; add the A1 allowable lower limit (−12 °C) | 1 day |
| 5 | Physical security events | EN 50600-2-5 | Out of scope for this platform unless an access-control integration is wanted; door contacts stay | — |

Items 2 and 3 together move ISA-18.2 from partial to compliant; item 1 moves EN 50600-2-3 to compliant for class 3; item 4 removes the last ASHRAE caveat.

## Appendix: alarm rules and categories

The threshold rules in force on 5 November 2026, as stored in the platform's rule table. Thresholds are the raise point; clear is the hysteresis point; dwell is the number of consecutive samples before raising.

| Rule | Metric | Raise | Clear | Dwell | Severity | Class | Category |
| --- | --- | --- | --- | --- | --- | --- | --- |
| inlet-temp-high | inlet temperature | > 27 °C | 25 | 2 | WARNING | alert | Environmental |
| inlet-temp-critical | inlet temperature | > 32 °C | 30 | 2 | CRITICAL | alarm | Environmental |
| inlet-temp-low | inlet temperature | < 18 °C | 19 | 5 | WARNING | alert | Environmental |
| ambient-temp-high | ambient temperature (probes) | > 27 °C | 25 | 3 | WARNING | alert | Environmental |
| ambient-temp-critical | ambient temperature | > 38 °C | 35 | 2 | CRITICAL | alarm | Environmental |
| ambient-temp-low | ambient temperature (probes) | < 18 °C | 19 | 5 | WARNING | alert | Environmental |
| facility-room-temp-high / critical | ambient temperature (plant rooms) | > 35 / > 40 °C | 33 / 37 | 3 / 2 | WARNING / CRITICAL | alert / alarm | Environmental |
| dew-point-high | dew point (derived) | > 15 °C | 14 | 2 | WARNING | alert | Environmental |
| dew-point-critical | dew point (derived) | > 17 °C | 16 | 2 | CRITICAL | alarm | Environmental |
| dew-point-low | dew point (derived) | < −9 °C | −7 | 5 | WARNING | alert | Environmental |
| humidity-high / critical | relative humidity | > 70 / > 80 % | 65 / 75 | 5 / 2 | WARNING / CRITICAL | alert / alarm | Environmental |
| humidity-low | relative humidity | < 20 % | 25 | 5 | WARNING | alert | Environmental |
| cpu-temp-high / critical | CPU temperature | > 85 / > 90 °C | 80 / 85 | 2 / 2 | WARNING / CRITICAL | alert / alarm | IT equipment |
| cpu-high / cpu-saturated | CPU utilisation | > 90 % sustained | 80 | 5 | WARNING / MAJOR | alert / alarm | IT equipment |
| memory-high | memory utilisation | > 90 % | 85 | 5 | WARNING | alert | IT equipment |
| psu-failure | PSU state | failed | — | 1 | MAJOR | alarm | IT equipment |
| pdu-load-high / critical | PDU load | > 80 / > 90 % of nameplate | 70 / 85 | 3 | MAJOR / CRITICAL | alarm | Power |
| power-load-high | load (UPS, switchgear, MCC, MPP) | > 90 % | 80 | 3 | MAJOR | alarm | Power |
| endpoint-unreachable | reachability | no response | — | collector debounce | MAJOR | alarm | Visibility |
| telemetry-stale | last sample age | > 600 s while polling succeeds | — | 1 | WARNING | alert | Visibility |

Category is ownership, the attribute ISA-18.2 calls the alarm's console or operator position: who takes the first five minutes. The rule in force since 4 November: a device's own parts (CPU, fans, supplies, memory, ports) belong to that device's owner; the air it breathes (inlet, ambient, humidity, dew point) is Environmental whichever device measured it; cooling plant is Cooling; the fabric (links, sessions) is Network; the monitoring's own sight of the plane is Visibility. An alarm raised as a consequence of a cooling cause keeps its category and carries the cause on its row and in its detail.
