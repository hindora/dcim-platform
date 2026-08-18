# 01 — Architecture Review

Reviewer stance: senior datacenter infrastructure architect. The job here is
accuracy, not agreement. Where the proposal matches how real DCIM/NMS platforms
are built, that is stated plainly and briefly. Where it does not, the difference
and the production-correct alternative are given before any code is written.

---

## 1. What the proposal gets right

These are not filler — they are the decisions that most in-house DCIM projects
get wrong, and the proposal already has them right.

**1.1 Three-tier split (Go collection / Python business logic / React UI).**
This mirrors how commercial platforms are actually built. Sunbird, Nlyte,
EkkoSense, and every large in-house NMS separate the *poller plane* from the
*application plane*, because they have different failure modes, different
scaling axes, and different release cadences. A poller that must not miss a
30-second tick cannot share a process with an analytics query that takes 4
seconds. Correct.

**1.2 Canonical telemetry model.** This is the single most important
architectural decision in the whole system and the proposal identifies it
correctly. The rule "the DCIM backend must not depend on SNMP OIDs, Redfish JSON
structure, gNMI paths, or BACnet object IDs" is exactly right, and it is the
thing that makes protocol #6 cheap instead of catastrophic.

**1.3 Current state separated from historical telemetry.** Correct, and for the
right reason. A dashboard that computes "is this device up" with a
`SELECT ... ORDER BY ts DESC LIMIT 1` over a hypertable will be fine at 600
devices and will fall over at 6,000. Keep a small, hot, mutable `device_state`
table.

**1.4 Dedicated trap path.** Correct. Traps are not polling and must not be
modelled as polling. In this simulator that matters more than usual: the trap
engine defines 103 trap definitions and rewrites the OID to the *vendor's* MIB
at send time, so an over-current on an APC rPDU leaves as
`1.3.6.1.4.1.318.0.276` (`rPDUOverload`) while the same logical condition on a
Raritan PX leaves as `1.3.6.1.4.1.13742.6.0.65`
(`overCurrentProtectorSensorStateChange`). A receiver that only understands one
placeholder enterprise tree will silently drop most of them.
(Source: `core/trap_definitions.py`, `core/vendor_oids.py`.)

**1.5 TimescaleDB rather than a bespoke time-series store.** Correct for this
scale. At 664 devices × ~40 metrics ÷ 30 s ≈ **900 samples/s**, Timescale on one
node is comfortable and gives you SQL joins against the inventory — which is
exactly what DCIM analytics needs and what Prometheus would make painful.

**1.6 "Avoid unnecessary microservices."** Correct instinct, and it should be
held to even where this review adds a component.

---

## 2. Critical findings

### A1 — The collector must not write to the database directly

**What the proposal says.** The diagram routes `Go Collector → PostgreSQL /
TimescaleDB → FastAPI`. Rule 13 separately asks for "a stable and versioned
contract between Go Collector and DCIM".

**Why it is wrong.** Those two statements are in conflict. If the collector
writes to the database, then *the database schema is the contract*. That means:

- Two independently deployed services own the same DDL. Every Alembic migration
  becomes a coordinated release of a Go binary and a Python service. This is the
  single most common source of paralysis in systems built this way.
- **No backpressure.** If Timescale is compacting, or a chunk lock is held, or
  the disk is slow, the collector's write path blocks. Blocked writes back up
  into the scheduler, polls are missed, and you lose data you already
  successfully collected off the wire. There is nowhere to buffer.
- **No replay.** Restart the backend and any in-flight work is gone. There is no
  way to re-run the enrichment/rule stage over the last 10 minutes after fixing
  a rule bug.
- **The alarm engine has no trigger.** Rules live in FastAPI (correct), but the
  data lands in Postgres without FastAPI being told. You are then forced into
  either polling the DB every second (wasteful, laggy) or `LISTEN/NOTIFY`
  (which drops messages when no listener is connected, and has an 8 kB payload
  limit — people discover both facts in production).
- **The collector cannot be sharded.** Running two collector instances against
  overlapping device sets produces racing upserts on `device_state` with no
  arbiter.

**The fix.** Insert a durable stream between the planes. The collector
*publishes*; a Python **ingest worker** (part of the backend deployment, but a
separate process from the API) is the only writer to the database.

```
Go Collector ──publish──▶  Redis Streams  ──consume──▶  Ingest Worker ──▶ PostgreSQL (state)
                          telemetry.v1                        │           TimescaleDB (history)
                          events.v1                           │
                          devicestate.v1                      └──▶ Redis Pub/Sub ──▶ FastAPI WS
```

**Why Redis Streams and not Kafka or NATS.** Redis is already in the accepted
stack, so this adds *zero* new infrastructure — which respects rule 12. Redis
Streams give you consumer groups, at-least-once delivery, acknowledgement,
pending-entry inspection, and `MAXLEN ~` capping. At 900 samples/s (batched, so
realistically a few dozen stream entries per second) this is nowhere near
Redis's limits.

- Choose **NATS JetStream** instead if you later want multiple independent
  consumers with different retention, or multi-site collection. It is the better
  long-term fit and the migration is contained to one publisher and one consumer.
- **Do not choose Kafka.** At this scale it is operational cost with no benefit.

**Honest counter-argument.** Direct writes are simpler, and at 664 devices
PostgreSQL will absolutely keep up. If this were a throwaway lab tool, direct
writes would be defensible. It is not: the stated goal is "the same DCIM
platform can later monitor real datacenter infrastructure". The moment there are
two collectors, or a second consumer of telemetry, or a schema migration during
a shift, the direct-write design costs more than the broker ever did.

---

### A2 — There is no device-endpoint model, and there must be

**What the proposal says.** `Device` has a `Protocol` and a management IP. The
example device config is `device_id / protocol / host / port / community`.

**Why it is wrong.** One inventory device routinely has *several* independent
protocol endpoints, and in this simulator that is not an edge case, it is the
normal case:

| Inventory device | Endpoint 1 | Endpoint 2 | Endpoint 3 |
|---|---|---|---|
| Server `SRV01-DC1-HA-R2-01` | SNMP on the **production** NIC `10.50.11.19` (the OS net-snmp agent) | SNMP on the **BMC** `10.51.11.25` (iDRAC/iLO/XCC agent, a different MIB subtree) | Redfish on the BMC `https://10.51.11.25:8443` |
| Leaf switch | SNMP on the OOB mgmt IP | gNMI on the same mgmt IP, port 57400 | — |
| CRAH | SNMP (native comm card) | BACnet/IP port 47808 | — |
| Chiller | *(no SNMP at all)* | BACnet/IP port 47808 | — |
| Belimo valve | — | BACnet **MS/TP** — no IP of its own: `(network, MAC)` behind a Loytec router's IP | — |
| Chilled-water transmitter | — | — | Modbus **RTU slave** — no IP: unit-id behind a Moxa gateway IP |
| Utility feed meter | — | — | Modbus/TCP only |

Two of those rows are the important ones: **the BACnet MS/TP devices and the
Modbus RTU slaves have no IP address at all.** A model of the form
`device.host + device.port` cannot represent them, and they are not exotic — a
real BMS is mostly MS/TP trunks behind IP routers, and real plant instrumentation
is mostly 4-20 mA transmitters behind a Modbus gateway. This is the realistic
case, and the simulator models it deliberately
(`Device.mstp_net/mstp_mac/mstp_router_ip`, `Device.modbus_role/modbus_unit_id/
modbus_gateway_ip` in `core/device_manager.py`).

**The fix.** `device` 1—N `device_endpoint`:

```
device_endpoint
  id, device_id, protocol, role,
  address        -- IP, or the parent gateway/router IP for sub-devices
  port,
  addressing     -- JSONB: {"community":"10.51.11.25"}
                 --        {"bacnet_instance": 2001}
                 --        {"bacnet_network": 2001, "bacnet_mac": 12, "via_endpoint_id": ...}
                 --        {"modbus_unit_id": 7, "via_endpoint_id": ...}
                 --        {"gnmi_target": "10.51.11.25"}
  credential_id,
  poll_profile_id,
  enabled, admin_state
```

`role` distinguishes `os_agent` / `bmc` / `native_card` / `field_device`.
`via_endpoint_id` is a self-reference that expresses "reached through this
gateway", which is what makes gateway/router modelling work — and it is also
what lets the alarm engine say "the Moxa gateway is down, therefore these 6
transmitters are unreachable *as a symptom*, not as 6 independent faults".

Collector assignment, credentials, poll interval, and health are all properties
of the **endpoint**, not of the device. Device health is then *derived* from its
endpoints, which is the correct direction.

---

### A3 — `Connection(interface → interface)` cannot express this datacenter

**What the proposal says.** The model list has `Interface` and `Connection`, and
the relationship examples are `UPS → PDU → Server`, `Chiller → CRAH`,
`CDU → Rack`.

**Why it is wrong.** Those examples are not interface-to-interface links. The
simulator's own topology export makes the point concretely — 2566 edges across
**five distinct layers** (`topologies/dual_dc_enterprise.json`):

| Layer | Edge count | What the endpoints actually are |
|---|---:|---|
| `power` | 1080 | PDU **outlet** → device **PSU inlet**. C13/C19 into C14/C20. Not interfaces. |
| `management` | 644 | mgmt interface → OOB switch port |
| `production` | 436 | data interface → data interface |
| `cooling` | 356 | chiller → CHWP → CRAH/CDU → rack. No ports at all — a hydronic relation. |
| `fieldbus` | 50 | Moxa gateway → RTU slave, Loytec router → MS/TP device. Addressed, not cabled. |

Forcing power cords into an interface table is exactly the mistake that makes
power topology unbuildable later. A cord terminates on a *specific outlet* with a
*specific connector type* and consumes that outlet's capacity — the simulator
already enforces this (`Device.outlets` / `Device.psus`, and the comment in
`core/device_manager.py` explicitly notes the cord's termination lives on the
edge, not on the device).

**The fix.** One polymorphic `connection` table with a `layer` enum and typed
endpoint references:

```
connection
  id, layer, link_type,
  a_device_id, a_termination_type, a_termination_id,   -- 'interface' | 'outlet' | 'psu' | 'none'
  b_device_id, b_termination_type, b_termination_id,
  attributes JSONB,        -- media, cord type, pipe loop id
  redundancy_side,         -- 'A' | 'B' | NULL
  admin_state, oper_state
```

Do **not** create `network_connection`, `power_connection`, `cooling_connection`
as three tables. Correlation and topology traversal want one graph with a layer
filter; three tables means every traversal is a three-way UNION.

`redundancy_side` is not optional. Without it you cannot answer the only question
that matters during an event: *"is this load still fed from the other side?"*

---

## 3. High-severity findings

### A4 — Redfish and BACnet are not poll-only

**Redfish.** The simulator implements a real `EventService`:
`/redfish/v1/EventService`, `/redfish/v1/EventService/Subscriptions` (GET/POST/
DELETE) and `EventService.SubmitTestEvent` (`simulator/redfish_device.py`,
`api/routers/redfish.py`). Real BMCs (iDRAC 9, iLO 5/6, XCC) do the same.
Polling `/Thermal` and `/Power` every 30 s per BMC is the expensive, laggy way to
get information the BMC will happily push.

**The fix.** The collector runs an **HTTPS event receiver** and registers a
subscription per BMC with `Destination = https://<collector>:<port>/redfish-events`.
On startup it reconciles: list existing subscriptions, delete stale ones pointing
at dead collectors, create missing ones. Then poll `/Thermal` + `/Power` at a
*slow* cadence (60 s) for the numeric sensor stream, and take state changes from
events. This halves BMC load and cuts alarm latency from 30 s to sub-second.

This is also how it works in production: nobody polls 5,000 iDRACs for health
state; they subscribe.

**BACnet.** Same argument via `SubscribeCOV` — Change-Of-Value is the native
BACnet push mechanism and every plant controller supports it for binary points.
Poll the analog inputs with `ReadPropertyMultiple`, subscribe COV to the binary
alarm points (`Alarm_HighPressure`, `Alarm_Leak`, `Alarm_FlowLoss`,
`Alarm_HighCHWSupply`, ...). If COV proves troublesome against the simulator,
poll the BI points at 5 s — but design the adapter so the event path exists.

### A5 — No metric registry, no counter semantics

Two separate gaps.

**No registry.** "Canonical metric" is asserted but never defined. Without a
single shipped dictionary, the SNMP adapter emits `cpu_util`, the Redfish adapter
emits `cpu_utilization`, the gNMI adapter emits `cpu_pct`, and the DCIM ends up
with the OID problem it was designed to avoid, one level up. See
`06-metric-registry.md`. The registry must define, per metric: key, display name,
unit, value type, aggregation semantics, valid range, staleness horizon, and
which device types carry it. It should be one YAML file, code-generated into Go
constants and Python enums, so a typo is a build error in both languages.

**No counter semantics.** `ifHCInOctets` is a 64-bit monotonic counter. gNMI
`/interfaces/interface/state/counters/in-octets` is the same. The proposal's
telemetry model has `value` and nothing else, so a chart of it shows a ramp, not
throughput. You need:

- `value_type: gauge | counter | delta | bool | string`
- rate derivation at **ingest**, not in the chart query, storing both the raw
  counter and the derived rate;
- **wrap detection** (32-bit counters still exist on old gear) and
  **discontinuity detection** — a `sysUpTime` that went backwards, or a gNMI
  stream reconnect, means the counter reset and the next delta must be
  *discarded*, not emitted as a 4-billion-byte spike. Every NMS that skipped this
  has a once-a-week spike in its graphs.

### A6 — The alarm engine as specified will be unusable

`CPU > 80 → WARNING, CPU > 90 → CRITICAL` is a threshold, not an alarm engine.
At 664 devices, a metric sitting on 80.0 will raise and clear a few hundred times
an hour. What is missing:

1. **Dwell / debounce.** Raise only after the condition holds for N consecutive
   evaluations or T seconds. Reasonable defaults: 3 samples or 90 s for thermal,
   2 samples for power, immediate for binary faults and leaks.
2. **Hysteresis.** Clear at a *different* threshold than raise (raise at 80,
   clear at 75). Without a deadband you get flapping by construction.
3. **Alarm key and dedup.** An alarm is not an event row. It is a *stateful
   object* keyed by `(device_id, alarm_type, instance)` — where `instance` is
   the interface, sensor, phase, or BACnet object. A unique partial index on that
   key `WHERE state <> 'CLEARED'` makes raise/update/clear idempotent, which is
   what lets you replay the stream safely.
4. **Explicit clears from traps.** This simulator emits paired clear traps
   (`cpuNormal`, `temperatureNormal`, `sensorHumidityNormal`,
   `upsUtilityRestored`, `upsBatteryNormal`, `upsOutputNormal`, ...) on their own
   distinct OIDs precisely so a receiver can treat them as clears. They must
   resolve to the *same alarm key* as the raise, or the alarm list only ever
   grows.
5. **Dependency suppression.** When an OOB switch drops, the 20 devices behind
   it go unreachable. Emitting 21 critical alarms is a defect, not a feature.
   Traverse the `management` layer, mark the children `symptom_of` the parent,
   and count them as one incident. Same logic on the `power` layer (a PDU trip
   takes its cord-fed servers) and the `cooling` layer.
6. **Severity model with defined precedence**, plus `prev_severity` so the UI can
   show escalation, and `first_seen / last_seen / occurrence_count`.

### A7 — Static collector config vs a fleet that changes at runtime

The proposal's device configuration is a YAML file. This simulator has a **fleet
lifecycle engine** that commissions and decommissions devices while running
(`POST /api/fleet/start`, `/advance`, `/provision-rack`, `/provision-hall` in
`api/routers/fleet.py`) and hot-adds them into the Redfish/gNMI/BACnet
controllers. A file-based device list goes stale within minutes, and — worse —
hides the *interesting* behaviour, which is the DCIM observing lifecycle change.

**The fix — invert the flow.** The DCIM database is the source of truth for what
exists; the collector *pulls its assignment*:

```
GET /api/v1/collector/assignments?collector_id=col-1
If-None-Match: "<etag>"
→ 200 { version, endpoints: [...] }   |   304 Not Modified
```

Polled every 30 s with an ETag, plus a Redis pub/sub nudge on inventory change
for sub-second reaction. The collector diffs the returned set against its running
scheduler and starts/stops jobs. `collector.yaml` then holds only what a
*process* needs — bind addresses, DB/Redis URLs, worker counts, timeouts — never
the device list. This also gives you sharding for free: the assignment endpoint
decides which collector owns which endpoints.

---

## 4. Medium-severity findings

### A8 — Modbus/TCP is omitted, and it is load-bearing

The simulator serves **Modbus/TCP on port 502** for 30 electrical devices plus
2 Moxa gateways fronting 12 IP-less plant instruments (`simulator/
modbus_controller.py`, `core/modbus_register_map.py`). Critically, the
**utility feed / revenue meter is Modbus-only** — the device type comment in
`core/device_manager.py` says so outright, and there is no SNMP path to it. That
means without a Modbus adapter you cannot measure site energy in, which means you
cannot compute PUE from the meter, which is the number the whole
"Analytics → PUE" section exists to produce.

This is also true of real datacenters: switchgear, revenue meters and power
quality meters are Modbus (or IEC 61850 in larger plants); BACnet is the BMS
plane; SNMP is the IT plane. Any DCIM that only speaks SNMP + BACnet has a hole
exactly where the electrical truth lives.

**Recommendation:** Modbus/TCP adapter in Phase 2, same priority as BACnet.

**sFlow** (simulator port 6343) is optional. Flow analytics is a different
product category; include the receiver only if you actually want top-talkers.

### A9 — Discovery must produce candidates, not inventory

"Every discovered device must become an inventory object" is the wrong rule.
Real DCIM keeps a `discovery_candidate` staging table; promotion to inventory is
an explicit action (manual, or by an auto-promote policy scoped to a subnet).
Otherwise a single broad sweep fills your CMDB with printers.

Second, and more important: **physical placement is not discoverable.** No
protocol tells you the device is in Row 2, Rack 1, U18. In this simulator that
information lives in the topology export (`GET /api/topology/export`) and in the
`sysLocation` string the SNMP agent synthesises. So the onboarding path is:

1. **Seed import** from `GET /api/topology/export` → full inventory with
   placement, links across all 5 layers, outlets, PSUs, interfaces.
2. **Protocol discovery** thereafter used for *reconciliation* — "the network
   says there is an SNMP agent at 10.51.11.42 that inventory does not know
   about" → candidate row + a drift alarm.

That is exactly how real DCIM deployments run (import from CMDB/spreadsheet, then
verify by polling), and it is the only way to get placement right.

### A10 — WebSocket fan-out is unscoped

Broadcasting every telemetry update to every client is 664 devices × ~40 metrics
≈ 26,000 values per cycle. The browser cannot render it and does not want it.

**The fix.** Client-declared subscriptions
(`{"op":"subscribe","topics":["device:abc123","alarms","dashboard"]}`),
server-side coalescing on a 1-second tick (send the latest value per key, not
every sample), never send history over WS, and use Redis pub/sub as the fan-out
bus so more than one uvicorn worker works at all. That last point is not optional
the moment you run `--workers 2`.

---

## 5. Smaller corrections worth making before code

| # | Issue | Correction |
|---|---|---|
| B1 | Timestamps | Carry **two**: `observed_at` (device/protocol timestamp — gNMI gives nanoseconds; BACnet gives none, so stamp at read; traps carry `sysUpTime`, not wall clock) and `collected_at` (collector wall clock). Charts use `observed_at`; collection-latency monitoring uses the difference. |
| B2 | Timescale writes | Never row-by-row `INSERT`. Batch 1–5k rows and use `COPY` (`asyncpg.copy_records_to_table`). Set `chunk_time_interval` to ~1 day, compress after 7 d, continuous aggregates at 1 m / 5 m / 1 h, retention 90 d raw. Without CAGGs a 30-day chart table-scans. |
| B3 | Metric column | Store `metric_id smallint` FK to a metric dimension table, not a text label per row. Text labels inflate the hypertable and its indexes for no benefit. |
| B4 | Poll jitter | 664 endpoints on a 30 s schedule will all fire at `t=0` and thunder. Hash the endpoint id into the interval to spread phase. |
| B5 | Per-protocol concurrency | Redfish is HTTPS-per-BMC (expensive handshakes → connection pool + keepalive, cap in-flight per host at 1–2). BACnet/IP is UDP with one outstanding request per device unless you track invoke IDs. SNMP tolerates high fan-out. Give each protocol its own semaphore, not one global pool. |
| B6 | PUE | Must be computed from **energy (kWh) integrated over an interval**, not from an instantaneous power ratio. The simulator already persists kWh. Compute it server-side in one place; never in the React layer. |
| B7 | FastAPI async | SQLAlchemy 2.0 async + `asyncpg`. Any analytic taking >200 ms goes to a worker (arq on Redis), not the request path — rule 15 demands it and it is easy to violate accidentally with a lazy-loading relationship. |
| B8 | Credentials | A `credential` table referenced by endpoint, encrypted at rest (app-level AES-GCM with a key from env/KMS, or pgcrypto), never returned by any API, redacted in every log line. The simulator's defaults (`admin`/`password` for Redfish, community == IP, insecure gNMI) are fine for a simulator and must not leak into the DCIM's design. |
| B9 | API authz | JWT + RBAC (`viewer` / `operator` / `admin`). Alarm acknowledge, device write and fault injection are privileged operations. |
| B10 | Migrations | Alembic from commit 1. Once the ingest worker is the only writer, migrations are a single-service concern — which is half the point of finding A1. |
| B11 | Collector identity | A `collector_instance` table with heartbeat, version and owned shard. The UI needs "is my collection plane healthy" as a first-class view; `collector_status` is already in the proposed WS event list, so give it a backing table. |
| B12 | Phase order | The proposal builds all five protocols (Phase 2) before the pipeline (Phase 3). Build **one vertical slice end-to-end first** — SNMP → contract → ingest → state → API → one UI tile. Every contract flaw surfaces in week 2 instead of week 8. |

---

## 6. Scale reality check

The proposal asks for "hundreds/thousands of devices". Against this simulator:

- 664 devices, ~900 canonical samples/s at 30 s cadence. Trivial for Go, trivial
  for Redis Streams, comfortable for a single Timescale node.
- The binding constraints are **not** CPU. They are:
  - **File descriptors** — one socket per Redfish BMC and per gNMI stream. This
    repository has already hit fd exhaustion on the *simulator* side at large
    fleet sizes; the collector will meet the same wall from the other direction.
    Pool aggressively, cap streams, raise `ulimit -n` explicitly and check it at
    boot.
  - **BACnet UDP serialisation** — no pipelining per device.
  - **TLS handshakes** — Redfish, if you do not reuse connections.
- 10,000 endpoints is reachable with the design above on one collector; beyond
  that, shard by `collector_id` in the assignment endpoint. That is the reason
  the assignment endpoint exists (A7).

---

## 7. Bottom line

Keep: the three-plane split, the canonical model, state/history separation, the
dedicated trap path, Timescale, and the "no unnecessary microservices" rule.

Change before writing code:

1. Put a **stream** between the collector and the database; the Python **ingest
   worker** is the sole writer. (A1)
2. Model **endpoints**, not `device.protocol`. (A2)
3. Model **one layered connection graph** with typed terminations. (A3)
4. Treat **Redfish EventService and BACnet COV** as first-class event sources. (A4)
5. Ship a **metric registry** with counter semantics. (A5)
6. Build a **real alarm engine** — dwell, hysteresis, alarm keys, dependency
   suppression. (A6)
7. Have the collector **pull assignments** from the DCIM API. (A7)
8. Add **Modbus/TCP**. (A8)

With those eight changes the architecture is production-shaped and will survive
the move from the simulator to real infrastructure without a rewrite.
