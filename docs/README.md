# DCIM Platform — Design Documentation Set

Target system: a DCIM/monitoring platform that consumes the **Datacenter Network
Simulator** as its device plane today, and real datacenter infrastructure later.

These documents are written against the *actual* simulator in this repository,
not against a generic idea of one. Every protocol quirk, port, addressing rule
and metric name cited here was read out of the source, and the origin is noted
so it can be re-verified.

## Verdict on the proposed architecture

**The macro shape is correct. Six things are wrong or missing badly enough to
cause a rewrite if built as specified.** See `01-architecture-review.md` for the
full finding list with severities. Summary:

| # | Finding | Severity |
|---|---|---|
| A1 | Go collector writes directly to PostgreSQL/TimescaleDB — two services own one schema, no backpressure, no replay, and the "stable versioned contract" ends up being a DB schema | **Critical** |
| A2 | No device-endpoint model — one inventory device can have several protocol endpoints (a server has an OS SNMP agent *and* a BMC SNMP agent *and* Redfish). A flat `Device.protocol` cannot express the simulator, let alone real gear | **Critical** |
| A3 | `Connection(interface → interface)` cannot represent power cords (outlet → PSU) or cooling/fieldbus relations. The simulator already has 5 topology layers and 2566 edges | **Critical** |
| A4 | Redfish and BACnet treated as poll-only. The simulator serves a real Redfish `EventService` with subscriptions, and BACnet has COV. Polling-only throws away the event path and inflates poll cost | **High** |
| A5 | No metric registry, no counter/gauge distinction, no rate derivation, no wrap/discontinuity handling | **High** |
| A6 | Alarm engine is raw thresholds — no dwell, no hysteresis, no alarm key/dedup, no dependency suppression. Produces an unusable alarm list at 664 devices | **High** |
| A7 | Static `collector.yaml` device list, but the simulator commissions/decommissions devices at runtime (fleet lifecycle) | **High** |
| A8 | Modbus/TCP and sFlow omitted — the utility-feed revenue meter and 12 plant instruments are **Modbus-only**; electrical analytics is incomplete without them | **Medium** |
| A9 | Discovery auto-creates inventory. Real DCIM discovers *candidates*; promotion is a decision. Physical placement is not discoverable at all | **Medium** |
| A10 | WebSocket fan-out unscoped — 664 devices × ~40 metrics would flood every client | **Medium** |

## Reading order

| Doc | Contents |
|---|---|
| `01-architecture-review.md` | Critique of the proposed architecture, finding by finding, with the fix |
| `02-target-architecture.md` | Corrected architecture, component responsibilities, deployment topology |
| `03-repository-layout.md` | Repo/monorepo structure for collector, backend, frontend, contracts |
| `04-data-model.md` | ER model + full PostgreSQL/TimescaleDB DDL |
| `05-telemetry-contract.md` | Canonical telemetry & event schema v1, transport, versioning policy |
| `06-metric-registry.md` | The canonical metric dictionary, grounded in simulator points |
| `07-collector-design.md` | Go collector: interfaces, scheduler, worker pool, health, config sync |
| `08-protocol-adapters.md` | Per protocol: simulator specifics, mapping tables, gotchas |
| `09-backend-design.md` | FastAPI layering, ingest worker, alarm engine, correlation, analytics |
| `10-api-spec.md` | REST API specification |
| `11-websocket-spec.md` | WebSocket protocol, subscriptions, fan-out |
| `12-frontend-spec.md` | React app structure, routes, views, state |
| `13-observability-security.md` | Metrics, logs, traces, health, credentials, authz |
| `14-testing-strategy.md` | Unit, integration, end-to-end, chaos |
| `15-implementation-roadmap.md` | Phased plan with exit criteria per phase |
| `16-simulator-integration.md` | **Verified** simulator facts: ports, addressing, endpoints, quirks |
| `17-operations-runbook.md` | Running it: restart procedures, alarm response, limits, known gaps |
| `18-alert-taxonomy.md` | The seven-category alarm taxonomy, why it replaced the five, and what it cost |

Start with `16-simulator-integration.md` if you want the concrete facts first;
start with `01` if you want the argument first.

## Asset & inventory module (`19`–`23`)

A second design set, written the same way: a review of the proposal against the
running system first, then the specification that follows from it. **Not built.**

| Doc | Contents |
|---|---|
| `19-asset-inventory-review.md` | Critique of the 27-section asset plan, finding by finding (B1–B11), with a section-by-section scorecard |
| `20-asset-data-model.md` | Schema delta: columns on `device`, seven new tables, migration order `0043`–`0050` |
| `21-asset-api-spec.md` | REST additions — all additive; no existing endpoint changes shape |
| `22-asset-frontend-spec.md` | The `/assets` workspace. **§1 is the scope boundary: no page outside `/assets` changes** |
| `23-asset-implementation-plan.md` | Six phases with checkable exit criteria, plus a risk register |

The headline finding: **roughly sixty percent of the proposed module already
exists and runs.** §21 of the proposal would build a parallel `assets` table
duplicating seven live tables and giving every asset a second identity — the one
thing §24 of the same proposal exists to prevent. `19` B1 is the argument; `20`
§1 is the rule that replaces it.

Start with `19` — `20`–`23` only make sense as its consequences.

## Anomaly detection (`28`–`33`)

A design for detecting faults in the cooling plant, terminal units and the
devices they serve, researched against current industry practice and written
against the running platform and simulator, with a full build plan. **Not built.**

| Doc | Contents |
|---|---|
| `28-anomaly-detection-design.md` | Layered detection (data quality, hard thresholds, state-gated rules, residual + EWMA/CUSUM, peer comparison), per-device-class detector catalogue, cooling-chain incident correlation, advisory queue under ISA-18.2 budgets, the LLM verdict, codebase fit, simulator gaps, event-level evaluation and six phases |
| `29-anomaly-data-model.md` | Seven raw-SQL tables (no hypertables), DDL, indexes, retention and sizing, jsonb shapes, detector seed rows, Redis keys and locks, settings, migrations `0111`–`0114` |
| `30-anomaly-api-spec.md` | `/api/v1/anomalies` endpoints, role matrix, feedback state machine, expected-band rules, explainer guardrails, websocket events, alarm and Jira flow. Additive only |
| `31-anomaly-frontend-spec.md` | Triage view at `/analytics?view=anomaly`, finding and incident detail, expected band on device telemetry, derived-alarm evidence panel, detector settings, copy deck |
| `32-anomaly-simulator-changes.md` | Simulator ground truth: truth/wire split, sensor faults, degradation, ramps, weather and setpoint events, injection ledger, influence export, bug fixes, named campaign library |
| `33-anomaly-implementation-plan.md` | Phases 0–5 with tasks, files, estimates, tests, campaigns and exit criteria; consolidated detector table; evaluation harness; rollout; risk register; open questions |

The headline finding: **no LLM in the detection path.** Rules and classical
statistics detect; an optional, evidence-bound LLM may explain an incident
after the fact, and the system must work fully with it switched off.
