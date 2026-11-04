# 02 — Target Architecture

This document describes the architecture **as built**. The first version came
from findings A1–A10 in `01-architecture-review.md`; this revision brings it in
line with the code after the collector fleet (docs/26), digital twin (docs/27),
anomaly detection (docs/28–33), Jira (docs/25) and power (docs/34) work.
§8 lists what changed from the original target and why.

Detail lives in the per-area documents. This one shows the main parts, how
data moves between them, and the rules about who may do what.

---

## 1. System diagram

```
┌──────────────────────────── DEVICE PLANE (simulator today, real DC later) ────────────────────────────┐
│                                                                                                       │
│   SNMP v2c/v3        gNMI               BACnet/IP              Redfish BMCs          Modbus/TCP        │
│   udp/161            tcp/50051          udp/47808              tcp/8443              tcp/502           │
│   (one IP per        (Subscribe         (polled with RPM)      (polled; EventService (direct, or via   │
│    device)            STREAM or Get)                            push optional)        serial gateways) │
│        │                                                                                              │
│        └──── SNMP traps → collector udp/162 (dev fleet: 11621–11628, one per collector, unprivileged)  │
└───────────────────────────────────────────────┬───────────────────────────────────────────────────────┘
                                                │
┌───────────────────────────────────────────────▼───────────────────────────────────────────────────────┐
│                         GO COLLECTOR FLEET  (8 today: per site × management plane)                     │
│                         col-dc{1,2}-{prod,oob,bms,bms-2}                                               │
│                                                                                                        │
│   ┌── Inbound (push) ─────────────────┐   ┌── Outbound (poll) ────────────────────────────────────┐    │
│   │  SNMP trap receiver  udp/162  ON  │   │  Scheduler (per-endpoint, jittered) + worker pool     │    │
│   │  Redfish event rcvr  tcp/9143 OFF │   │  Adapters: snmp · gnmi · bacnet · redfish · modbus ·  │    │
│   │  (no BACnet COV receiver: BACnet  │   │            provider (colo / vendor REST APIs)         │    │
│   │   is polled, FDR optional)        │   │  Throttle, capacity limits, preflight checks          │    │
│   └───────────────┬───────────────────┘   └───────────────────────┬───────────────────────────────┘    │
│                   └───────────────┬───────────────────────────────┘                                    │
│                                   ▼                                                                    │
│      Mapping bundle (contracts/mappings/*.yaml) → Normalizer → canonical Telemetry/Event/EndpointState │
│                                   │                                                                    │
│   Assignment client ◀─ GET /api/v1/collector/assignments (ETag, every 30 s)                            │
│   Remote config, signed self-update + rollback, device-action runner, discovery runner                 │
│   Credentials: config, Vault, or sealed box · Health tracker per endpoint                              │
└───────────────┬───────────────────────────────────────────────────────┬────────────────────────────────┘
                │ transport: redis (default)                            │ transport: gateway (remote sites)
                │ XADD, msgpack                                         │ HTTPS POST, mTLS, msgpack+zstd,
                │ in-memory ring if Redis is down                       │ disk spool if the platform is down
                │                                                       ▼
                │                                      ┌──────────────────────────────────────┐
                │                                      │ Ingest gateway (in the API process)  │
                │                                      │ POST /collector/batches/{telemetry|  │
                │                                      │       events|endpointstate}          │
                │                                      │ stamps the PROVEN collector_id, XADD │
                │                                      └──────────────────┬───────────────────┘
                ▼                                                         ▼
                     ┌───────────────────────────────────────────┐
                     │              REDIS STREAMS                │
                     │   telemetry.v1  (MAXLEN 8 000)            │
                     │   events.v1     (MAXLEN 200 000)          │
                     │   endpointstate.v1 · collectorhb.v1       │
                     │   consumer group: dcim-ingest             │
                     └───────────────────┬───────────────────────┘
                                         │
┌────────────────────────────────────────▼───────────────────────────────────────────────────────────────┐
│                          DCIM BACKEND (one Python codebase, two process roles)                         │
│                                                                                                        │
│   ┌── ingest worker (2+) ─────────────────────────────┐  ┌── api (uvicorn) ───────────────────────────┐│
│   │ consume → ownership check → enrich → rates        │  │ REST /api/v1/...   WebSocket /ws (ticket)  ││
│   │        → COPY (temp table, ON CONFLICT) → hyper-  │  │ inventory/asset CRUD, alarm ack/clear,     ││
│   │          table → upsert current state             │  │ rule edits, device-action queue,           ││
│   │        → rule engine → alarm lifecycle            │  │ collector fleet admin (enroll, PKI,        ││
│   │        → PUBLISH dcim:ws:<topic>                  │  │ pools, drain, rollouts), discovery         ││
│   │ timers (advisory-locked, one worker acts):        │  │ scheduler loop, ingest gateway,            ││
│   │   staleness / dead-endpoint / trap-reconcile /    │  │ Jira webhooks, Prometheus /metrics         ││
│   │   late-root sweeps · platform monitor ·           │  │ never polls devices                        ││
│   │   collector visibility · assigner + rollout tick ·│  │ never makes an outbound integration call   ││
│   │   anomaly sweep + nightly baseline fit ·          │  └────────────────────┬───────────────────────┘│
│   │   asset snapshot · integration outbox dispatcher  │                       │                        │
│   └───────────────────────────────────────────────────┘                       │                        │
└──────────────────┬────────────────────────────────────────────────────────────┼────────────────────────┘
                   │                                                            │
     ┌─────────────▼──────────────┐        ┌────────────────────────────────────▼─┐   ┌──────────────────┐
     │  PostgreSQL 16 + Timescale │        │  Redis 7                             │   │ Jira / JSM, ops  │
     │  inventory · assets · state│        │   streams  (ingest bus)              │   │ targets (outbox, │
     │  alarms · anomalies        │        │   pub/sub  dcim:ws:* (WS fan-out)    │   │ HTTP, from the   │
     │  hypertable + CAGGs,       │        │   counter baselines dcim:ctr:*       │   │ ingest worker)   │
     │  compression + retention   │        │   noeviction, maxmemory 1 GB         │   └──────────────────┘
     │  advisory locks, leases    │        └──────────────────────────────────────┘
     └────────────────────────────┘
                   │
                   ▼
        ┌──────────────────────────────────────────────────────────────┐
        │  REACT 18 + TYPESCRIPT + VITE                                 │
        │  TanStack Query for REST, one WS client applying deltas       │
        │  canvas charts · three.js digital twin · @xyflow topology ·   │
        │  d3-geo world map                                             │
        └──────────────────────────────────────────────────────────────┘
```

Device-plane ports are the simulator's, checked against the running stack:
894 SNMP agents on udp/161 (v2c and v3 share the port), 310 Redfish BMCs on
tcp/8443, 46 gNMI targets on tcp/50051, 79 BACnet sockets on udp/47808, Modbus
on tcp/502. A real estate uses vendor defaults (gNMI is often 57400 or 9339,
Redfish 443).

The trap receiver defaults to udp/162 in code. The 8 dev collectors share one
WSL host, so each listens on its own unprivileged port (11621–11628), and the
simulator has to be told where to send traps (`POST /api/snmp/trap-receiver`).
In production, grant `cap_net_bind_service` and use 162.

---

## 2. Component responsibilities (hard boundaries)

| Component | Owns | Must never |
|---|---|---|
| **Go collector** | Wire protocols, sessions, retries; OID/path/object → canonical metric through the mapping bundle; endpoint reachability; trap reception; running discovery sweeps and device actions it has claimed; publishing to the stream or the gateway | Touch the database. Know what a rack is. Evaluate business thresholds. |
| **Ingest worker** | The **only writer of telemetry rows** (`ingest/writer.py`). Ownership check, enrichment, rate derivation, current-state upsert, rule evaluation, alarm lifecycle, WS fan-out. Also hosts every periodic job (listed in §1), each advisory-locked so only one of the workers acts on a given tick | Serve HTTP. Talk to devices. |
| **API (FastAPI)** | REST + WebSocket, authn/authz, validation, read models; writes to inventory, assets, alarm ack/clear, rule overrides, the device-action and discovery queues, collector fleet records; queues scheduled discovery sweeps; receives gateway batches and Jira webhooks | Poll devices. Make outbound calls to integrations. Write telemetry rows. Run long analytics inline. |
| **React** | Presentation, local UI state, chart rendering | Compute PUE, capacity, or any derived KPI. Poll REST in a loop for live data. |

There is **no separate task worker**. The jobs the first version gave to arq
ended up in other places (see §8).

The split of "device health" still holds: the **collector** decides whether an
endpoint can be reached, because only it knows a poll timed out. The **ingest
worker** decides whether a *value* is out of range. Both feed the same alarm
lifecycle.

---

## 3. Data flow, end to end

### 3.1 Polled telemetry

```
scheduler fires endpoint job
  → adapter.Collect(ctx, endpoint)               [protocol-specific]
  → mapping bundle → []Telemetry, canonical keys [normalize]
  → batch
  → transport=redis:   XADD telemetry.v1 (msgpack)
    transport=gateway: spool to disk → POST /collector/batches/telemetry
                       (zstd, mTLS) → API stamps collector_id → XADD
      ↓
  ingest worker XREADGROUP (group dcim-ingest)
  → ownership: drop batches from a collector that does not own the endpoint
               (grace period after a move; events exempt)
  → enrich: device → rack → row → room → dc, vendor, model, type
  → counters: derive rate against baseline in Redis (dcim:ctr:*, atomic Lua swap)
  → COPY into temp table → INSERT … ON CONFLICT DO NOTHING into the hypertable
  → upsert current state
  → rule engine (dwell + hysteresis) → alarm raise / update / clear
  → PUBLISH dcim:ws:device:<id>, room:<id>, dashboard, alarms …
      ↓
  WS hub (psubscribe dcim:ws:*) → each session's subscribed topics → browser
```

### 3.2 SNMP trap

```
device → udp/162 → trap receiver
  → decode PDU, source IP → endpoint → device
  → vendor trap table → canonical event_type, severity, is_clear
  → Event{} → events.v1
      ↓
  ingest worker
  → persist event row (always)
  → raise / update / clear alarm keyed (device, alarm_type, instance)
  → PUBLISH dcim:ws:events, alarms
      ↓
  trap-reconciliation sweep (timer)
  → the next poll decides: a polled value can raise and clear the same alarm,
    so a lost trap or a missing clear trap heals by itself
```

A trap speeds up detection; the poll is the record. Traps never become
telemetry values. Mixing the two gives sawtooth charts.

### 3.3 Redfish event

```
BMC → HTTPS POST → collector event receiver (tcp/9143, off by default)
  → map MessageId → canonical event_type → Event{} → events.v1
```

The downstream path is the same as for a trap. The alarm engine never learns
that Redfish exists. The receiver is off by default because the BMC must be
able to reach the collector, and on a management network built for outbound
polling it often cannot. Polling covers the same faults.

### 3.4 Inventory and assignments

```
discovery: schedule due → API loop queues a sweep row (FOR UPDATE SKIP LOCKED)
  → owning collector claims it → probes the range (SNMP / Redfish / gNMI)
  → candidates staged → operator promotes them to devices + endpoints
      ↓
  assigner (ingest worker timer, advisory-locked)
  → plans endpoint → collector by pool / zone, writes endpoint_assignment
    (leased ownership, damped moves, failover / failback recorded)
      ↓
  collector assignment client, every 30 s, If-None-Match
  → 304, or new list → start jobs for new endpoints, stop removed ones
```

`python -m app.importer.cli` loads a simulator topology into a **dev**
inventory. It is a fixture loader, not an operational path (migration 0077
removed its UI button). A DCIM must not depend on whatever generates the data.

### 3.5 Device action (write path)

```
operator → POST device action (outlet on/off/cycle, server reset)
  → API checks role, typed confirmation, impact preview, one-at-a-time lock
  → command row queued with an audit record
      ↓
  owning collector claims the command (same queue as upgrades)
  → SNMP SET on the PDU control OID / Redfish ComputerSystem.Reset
  → result reported back → audit row closed
```

The platform holds no usable device credentials and is not on the management
network; the collector is. Plant setpoints (CRAH, CDU, chiller) are left out on
purpose: they belong to the BMS, which owns the interlocks.

### 3.6 Integrations (Jira / ops)

```
alarm or anomaly action → outbox row written in the SAME transaction
  → dispatcher (ingest worker) claims rows FOR UPDATE SKIP LOCKED,
    serial per fingerprint so a clear never overtakes its raise
  → HTTP to Jira / JSM / ops target, with backoff and rate limits
Jira webhook → API → inbox → state applied back to the alarm
```

---

## 4. Deployment topology

**Development / demo (single host, next to the simulator).** `scripts/dev.sh`
runs every process on the host (WSL). Only Postgres and Redis run in Docker.

```
docker:   postgres  timescale/timescaledb-ha:pg16   :5432
          redis     redis:7-alpine, noeviction 1 GB :6379
dev.sh:   api       uvicorn app.main:app             :8000
          ingest1, ingest2   (INGEST_WORKERS, default 2)
          8 collectors, one per collector/configs/fleet/*.yaml, each with
            its own token, its own binary copy under var/fleet/install/<id>,
            and a restart loop (stands in for systemd so self-update and
            rollback work)
          ui        vite dev server                  :5173
```

`deploy/docker-compose.yml` describes the same stack as containers: api,
ingest with 2 replicas, one collector on `network_mode: host` and the Vite UI.
There is no worker service. The collector needs host networking because it
binds udp/162 and needs a routable source address for BACnet.

**Production shape (docs/26):**

- **Collectors:** one or more per site and management plane, placed on that
  network, grouped into pools and zones. Each enrolls with a one-time token and
  gets an mTLS client certificate from the platform CA (revocable). Collectors
  near the platform may XADD directly; remote ones use the gateway transport
  and its disk spool. Drain, decommission, preflight, version-skew checks and
  signed staged rollouts are all built.
- **Redis:** one instance to start. Sentinel or a managed Redis once the stream
  is the only buffer between planes.
- **Postgres/Timescale:** one primary plus a streaming replica for read-only
  analytics. Multi-node is not needed at this scale.
- **API:** several uvicorn processes are safe: the discovery loop claims rows
  with SKIP LOCKED, and WS fan-out goes through Redis pub/sub. There is no load
  balancer config for the API in the repo yet. The only nginx template is the
  mTLS front end for collectors (`deploy/collector-nginx.conf.template`).

---

## 5. Failure behaviour

| Failure | Behaviour |
|---|---|
| Database down | Ingest stops acking; the streams fill to MAXLEN; collectors keep polling. **`telemetry.v1` is capped at 8 000 entries**, so the database can only be down for a few minutes before old telemetry batches are trimmed. Events (200 000) last far longer. On recovery the backlog drains. |
| Redis down, `transport: redis` | The collector buffers telemetry in a bounded in-memory ring and drops the **oldest** when full. Events are never dropped. Drops show in the heartbeat and in Prometheus. |
| Platform unreachable, `transport: gateway` | Batches go to a disk spool with per-stream partitions and a byte budget. Over budget, whole segments are dropped, telemetry and events together. Heartbeats are never spooled. |
| Collector down | Heartbeat goes stale → `collector_stale` platform alarm, and the collector-visibility sweep marks that collector's endpoints `UNKNOWN`, not `OFFLINE`: you do not know. With pools, the assigner fails its endpoints over to a peer and fails them back later. |
| One device unreachable | Endpoint health degrades after N failures → `endpointstate.v1` → device `OFFLINE` → alarm, suppressed if a parent (OOB switch, gateway, router) is also down (`alarms/correlation.py`). A device with no endpoint at all reads `NOT_MONITORED`, not `UNKNOWN`. |
| Ingest worker down | Its pending entries are reclaimed by another worker with `XAUTOCLAIM` after an idle timeout. Run at least 2. Every timer job is advisory-locked, so the survivor carries all of them. |
| Ingest falling behind | The platform monitor raises `ingest_lag_high` / `ingest_stalled` from consumer-group lag. |
| Duplicate delivery | Downstream is idempotent: telemetry rows are keyed `(device_id, metric_id, instance, ts)` and inserted ON CONFLICT DO NOTHING; alarms key on the alarm key; gateway batches carry per-stream sequence numbers. At-least-once delivery is safe. |

---

## 6. Technology decisions and the reason for each

| Decision | Choice | Reason |
|---|---|---|
| Collector language | Go | Concurrency, static binary, no GIL. Cost: weak BACnet library ecosystem — see `08-protocol-adapters.md`. |
| Backend | FastAPI + SQLAlchemy 2.0 async + asyncpg + Pydantic v2 | Async all the way to the driver, or the async is decorative. |
| Bus | Redis Streams | No new infrastructure, consumer groups, replay, capped. Move to NATS JetStream if several consumers or sites arrive. |
| Bus serialisation | **msgpack keyed by field name**, schema in `contracts/schema/messages_v1.yaml`, Go and Python types generated by `contracts/codegen.py`; zstd on the gateway | Generated types on both sides without a `protoc` step. Unknown keys are ignored, which gives the forward compatibility protobuf tags would. A `.proto` is generated for later use; nothing reads it. |
| Collector → platform over untrusted networks | HTTPS gateway with mTLS | A remote collector must not hold the platform's Redis password, and the platform must stamp the collector identity itself, not trust the payload. |
| Time series | Timescale hypertable + stacked continuous aggregates `telemetry_1m → 5m → 1h → 1d`, compression, 30-day raw retention, 1m kept 7 days, hourly and daily kept forever | SQL joins to inventory; retention and rollups run inside the database, not in cron (migrations 0002, 0042, 0051–0053). Charts pick the tier by window. |
| Current state | Plain Postgres tables | Small, hot, updated in place. |
| Coordination | Postgres advisory locks, `FOR UPDATE SKIP LOCKED`, persisted leases | Everything that needs a lock already has a database transaction open; Redis locks would add a second source of truth. |
| Periodic jobs | Timers inside the ingest worker | That process already has a supervised lifecycle, a session factory and Redis. A task queue would have added a process and a dependency for jobs that are a few queries each. |
| Integrations | Transactional outbox / inbox | The alarm commit never waits on a ticketing system; order per fingerprint is kept. |
| Frontend data | TanStack Query (REST) + one WS client feeding a store | REST is the source of truth on mount; WS applies deltas. Never the reverse. |
| Charts | Custom canvas components (`TimeChart`, `Plot`, `Sparkline`) | Smooth with tens of thousands of points and styled to the design tokens. uPlot/ECharts were not needed. |
| 3D / graph / map | three.js + react-three-fiber, @xyflow/react, d3-geo | Digital twin, connectivity topology, world map. |

---

## 7. Explicitly rejected options

| Rejected | Why |
|---|---|
| Kafka | Operational weight with no benefit at ~900 samples/s. |
| Prometheus as the store | Pull model fights the collector design; labels are not an inventory; no joins to racks and rooms. Prometheus still scrapes the platform's own `/metrics`. |
| InfluxDB | Timescale keeps time series and inventory in one SQL engine. One less datastore. |
| One microservice per protocol | Adapters share the scheduler, health tracker and publisher. Splitting multiplies deployment cost for nothing. |
| Collector writing to the database | Finding A1. |
| gRPC from collector to backend instead of a stream | Loses the buffer. The gateway is HTTPS but writes straight into the same stream, so the buffer stays. |
| Server-Sent Events instead of WebSocket | The client sends subscription changes; WS fits. |
| A separate task-queue process (arq/Celery) | Planned, then not needed: see §6 "Periodic jobs". |
| The simulator API as the source of inventory | The importer stays a dev fixture loader; real onboarding is discovery. |

---

## 8. What changed from the original target

| Original target | As built | Why |
|---|---|---|
| Three backend roles incl. an arq task worker | Two roles: API and ingest | Rollups/retention went to Timescale policies; forecasts are computed on request; discovery is queued by the API and run by collectors; the remaining timers fit in the ingest worker. |
| Protobuf on the bus | msgpack, generated from a YAML schema | No `protoc` toolchain; same forward compatibility. |
| Collector always XADDs to Redis | Direct XADD **or** HTTPS gateway with mTLS and a disk spool | Remote collectors (docs/26 Phase 3). |
| One collector | Fleet of 8 with enrollment, PKI, pools, leased ownership, failover, drain, signed rollouts | docs/26 Phases 0–9. |
| Ingest is the only database writer | Ingest is the only **telemetry** writer | The API always had to write inventory, acks and queues. |
| Pub/sub channels `dcim:telemetry`, `dcim:alarm`, `dcim:assignments` | `dcim:ws:<topic>`; no assignments channel | Topic-per-subscription fan-out. Collectors re-fetch assignments every 30 s with an ETag; nothing wakes them early. |
| Redfish receiver tcp/9443, BACnet COV receiver udp/47809 | Redfish on tcp/9143, off by default; no COV receiver | Polling covers the same ground without needing inbound reachability through the management network. |
| Redis locks for leader election | Postgres advisory locks, SKIP LOCKED, leases | One source of truth. |
| Seed importer re-syncs on fleet change | Manual dev-only fixture loader | Migration 0077. |
| Charts: uPlot or ECharts | Custom canvas components | See §6. |
| — | Anomaly detection (docs/28–33), Jira/ops integrations (docs/25), device actions, digital twin and world map (docs/27), power metering and panel/outlet model (docs/34), platform self-monitoring, alarm rule editing, mapping bundles | Added after the first version. |
