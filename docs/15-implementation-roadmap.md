# 15 — Implementation Roadmap

The proposal's phase order builds all five protocols before the pipeline exists.
That maximises rework: every contract flaw is discovered five times. This plan
builds **one vertical slice end-to-end first**, then widens.

Durations assume one experienced full-stack engineer. Scale accordingly.

---

## Phase 0 — Foundations (1 week) — DELIVERED

| # | Deliverable | Exit criterion |
|---|---|---|
| 0.1 | Monorepo skeleton per `03-repository-layout.md` | `make build` produces all three artefacts |
| 0.2 | docker compose: Postgres+Timescale, Redis, empty api/ingest/collector | `docker compose up` healthy |
| 0.3 | `contracts/proto` v1 + codegen for Go and Python | generated code committed, CI diff check green |
| 0.4 | `contracts/metrics/registry.yaml` with the universal + compute + network groups | `mapcheck` passes |
| 0.5 | Alembic baseline: enums, hierarchy, device, endpoint, state, metric | `alembic upgrade head` on an empty DB |
| 0.6 | CI: lint, unit, contract stages | green on an empty repo |

**Do not skip 0.3/0.4.** Everything downstream depends on them being settled, and
they are cheap now and expensive later.

---

## Phase 1 — The vertical slice (2 weeks) — DELIVERED

One protocol, one metric family, all the way to a pixel.

| # | Deliverable | Exit criterion |
|---|---|---|
| 1.1 | Seed importer from `GET /api/topology/export` | 664 devices, racks, rooms, and all 2566 connections in the DB; re-running changes nothing |
| 1.2 | Endpoint derivation for SNMP (including the server double-endpoint) | every SNMP-capable device has the right endpoints with the right community |
| 1.3 | Collector skeleton: config, assignment client, scheduler, worker pool, health tracker, Redis publisher | polls a static assignment, publishes to `telemetry.v1` |
| 1.4 | SNMP adapter: system + interfaces + host resources | canonical samples on the stream for servers and switches |
| 1.5 | Ingest worker: consume → enrich → COPY → upsert state | rows in `telemetry_sample`, `device_state` populated |
| 1.6 | Timescale hypertable + compression + retention | `\d+ telemetry_sample` shows the policies |
| 1.7 | API: `/devices`, `/devices/{id}`, `/devices/{id}/state`, `/dashboard/summary` | p95 < 150 ms with 664 devices |
| 1.8 | React shell: layout, routing, auth, device list, device detail | live values visible in a browser |
| 1.9 | Assignment endpoint + ETag; collector switches to pulling | adding a device in the DB starts collection within 60 s |

**Phase gate:** a value changed in the simulator is visible in the browser within
one poll interval, with no manual step. Do not start Phase 2 until this holds.

---

## Phase 2 — Events and alarms (2 weeks) — DELIVERED

| # | Deliverable | Exit criterion |
|---|---|---|
| 2.1 | SNMP trap receiver + `traps.yaml` including vendor-rewritten OIDs | a trap from `POST /api/traps/send` becomes an event row |
| 2.2 | `events.v1` stream + event persistence | unresolvable sources recorded, not dropped |
| 2.3 | Alarm engine: keys, dwell, hysteresis, lifecycle | flapping metric produces exactly one alarm |
| 2.4 | Communication alarms from `EndpointState` | unplugging a device raises after 3 failures, clears on recovery |
| 2.5 | Default rule set + `/alarm-rules` CRUD + rule test endpoint | rules editable, test shows historical fire count |
| 2.6 | WebSocket: hub, subscriptions, coalescing, Redis fan-out | two browser tabs both update; 2 uvicorn workers both work |
| 2.7 | Alarm UI: list, filters, ack, drawer | operator can work an alarm end to end |

**Phase gate:** trap → alarm → browser in under 2 seconds; a clear trap clears the
same alarm.

> **Met.** Verified with real vendor OIDs on the wire: an APC `rPDUOverload`
> (318.0.276) becomes a CRITICAL `pdu_load_critical` alarm and arrives on a
> subscribed WebSocket; `rPDUNearOverloadCleared` (318.0.275) clears it. Source
> attribution fell back to the community string when the source address did not
> match, which is the path that matters for a wildcard-bound agent plane.
>
> Not yet built from this phase: 2.5's rule-test endpoint (rules are listed and
> can be enabled/disabled, but there is no "how often would this have fired"
> preview) and the alarm drawer's history timeline.

---

## Phase 3 — Full protocol coverage (3 weeks)

Order chosen by value-per-effort and by risk.

| # | Deliverable | Notes |
|---|---|---|
| 3.1 | DONE - **Redfish poller** (3 days) | biggest metric yield per device; 310 servers |
| 3.2 | DONE - **Redfish EventService** subscribe + receiver + reconciliation (3 days) | halves BMC load, cuts alarm latency |
| 3.3 | DONE - **BACnet/IP client** (6-8 days) | the schedule risk. Who-Is/I-Am, ReadProperty, ReadPropertyMultiple first; COV after. Timebox at 8 days — if the encoding work overruns, switch to the BACpypes3 side-process fallback and keep the contract identical. |
| 3.4 | DONE - BACnet MS/TP routed addressing + directed Who-Is identification (2 days) | required for valves and pump cards |
| 3.5 | DONE - **Modbus/TCP** (3 days) | utility meter is the PUE numerator; gateway unit-id fan-out |
| 3.6 | DONE - **gNMI** poller + STREAM (4 days) | interface counters at higher fidelity than SNMP |
| 3.7 | Interface identity normalisation (gNMI name ↔ SNMP ifIndex) (1 day) | otherwise one interface produces two series |
| 3.8 | Per-protocol integration tests | the table in `14-testing-strategy.md` §3 |

**Phase gate:** every device type in the simulator reports at least its primary
metrics; `SELECT device_type, count(DISTINCT metric_id) FROM ...` shows no
unexpected zeros.

---

## Phase 4 — DCIM depth (3 weeks)

| # | Deliverable | Exit criterion |
|---|---|---|
| 4.1 | Topology service: recursive CTEs, per-layer graph, caching | `/topology?layer=power&scope=room` < 400 ms |
| 4.2 | Correlation: dependency suppression with the redundancy check | OOB switch failure → 1 root + N symptoms; A-feed failure with a healthy B feed is **not** suppressed |
| 4.3 | Impact analysis (`/devices/{id}/impact`) | returns loads that would lose their last feed |
| 4.4 | Rack elevation endpoint + rack view UI | one request renders 42 U with overlays |
| 4.5 | Floor plan + heat map | racks positioned, temperature overlay |
| 4.6 | Topology UI (4 layers) | live state, no re-layout on update |
| 4.7 | Continuous aggregates + history endpoint + charts | 30-day chart < 300 ms |
| 4.8 | Staleness detection | reachable-but-silent endpoint alarms |
| 4.9 | Discovery: candidates, promotion, drift alarm | a device answering SNMP but absent from inventory is flagged |

**Phase gate:** an operator can go from a dashboard alarm to the responsible
rack, see the power and cooling chain, and identify the blast radius, without
leaving the UI.

---

## Phase 5 — Analytics (2 weeks)

| # | Deliverable | Exit criterion |
|---|---|---|
| 5.1 | Power analytics: per-chain load, redundancy verdict, phase balance | `/power/chain/{id}` returns `N+1 / single_feed / no_feed` |
| 5.2 | Cooling analytics: loop ΔT, plant capacity vs load, chiller staging | plant view matches the simulator's own plant health |
| 5.3 | PUE (energy-based, with method and level reported) | plausible 1.2–2.0; degrades gracefully to `method=power` |
| 5.4 | Capacity: power/cooling/space/ports, p95-based, per rack/room/DC | `/capacity` returns all four constraints with the binding one flagged |
| 5.5 | Thermal analytics: ΔT, hot spots, high-return vs high-supply distinction | hot spot detection fires on a real thermal event |
| 5.6 | Forecasting (linear + Holt-Winters, with an explicit insufficient-history state) | no forecast shown below 14 days of data |
| 5.7 | Analytics UI | charts for each of the above |

---

## Phase 6 — Hardening and scale (2 weeks)

| # | Deliverable | Exit criterion |
|---|---|---|
| 6.1 | Full observability: metrics, tracing, platform alarms, collector health page | `dcim_ingest_lag_seconds` alerting works |
| 6.2 | Security pass: credential encryption, RBAC, audit log, redaction tests | a credential never appears in any log or response |
| 6.3 | Load test to 5,000 endpoints | criteria in `14-testing-strategy.md` §6 |
| 6.4 | Chaos suite | all rows in §7 pass |
| 6.5 | Collector sharding by `collector_id` | 2 collectors split the fleet with no overlap |
| 6.6 | Soak 72 h | flat fds, goroutines, memory |
| 6.7 | Runbooks + operator documentation | someone else can operate it |

---

## Phase 7 — Advanced (ongoing)

Enabled by, not blocking, the above:

- Fault correlation chains across layers (CDU leak → rack cooling → inlet rise →
  server thermal), expressed as declarative correlation rules over the topology
  graph rather than hardcoded paths.
- Predictive maintenance on run-hours, vibration, filter ΔP, approach
  temperature, and battery health — the simulator already carries all of these.
- Thermal modelling correlating rack power, airflow and inlet temperature.
- Simulator lifecycle integration: observe commissioning as an inventory event
  with its own timeline.
- Multi-site: collector per site, one DCIM, scoped users.
- Reporting: scheduled PDF/CSV for capacity and energy.

---

## Critical path and risk register

```
0.3/0.4 contract ─▶ 1.3 collector ─▶ 1.5 ingest ─▶ 1.7 API ─▶ 1.8 UI
                          │
                          └─▶ 2.1 traps ─▶ 2.3 alarms ─▶ 2.6 WS ─▶ 2.7 alarm UI
                                                │
                                                └─▶ 4.2 correlation (needs 4.1 topology)
```

| Risk | Impact | Mitigation |
|---|---|---|
| **BACnet in Go** | Phase 3 slips | RESOLVED: written in Go, no fallback needed. The codec is pinned against the device side by vectors generated from `core.bacnet_object_model`, which is what made it tractable - the risk was never the encoding, it was having no independent way to tell a wrong frame from a right one |
| Contract churn after Phase 1 | rework across both planes | Phase 1 exists specifically to shake this out before four more adapters depend on it |
| Timescale query performance | slow charts | CAGGs in Phase 1 (1.6), not deferred |
| Alarm noise | the product becomes unusable and gets ignored | dwell + hysteresis in 2.3, correlation in 4.2, and the rule-test endpoint in 2.5 before any rule ships |
| Simulator-specific assumptions leaking into the DCIM | painful port to real hardware | all protocol specifics in `contracts/mappings/*.yaml`; a review checklist item on every adapter PR |
| fd exhaustion at scale | collector dies under load | `max_open_files` checked at boot; connection pooling from day one; soak test in 6.6 |

---

## Definition of done, per component

A component is done when it has: production error handling (no bare `except`, no
ignored `err`), structured logging at the right levels, metrics, unit tests,
an integration test against the simulator, configuration with no hardcoded
values, and an entry in the operator runbook.

"It works on my machine against one device" is Phase 1 of that list, not the
whole of it.
