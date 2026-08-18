# 02 — Target Architecture

This is the corrected architecture, incorporating findings A1–A10 from
`01-architecture-review.md`. Everything else from the original proposal stands.

---

## 1. System diagram

```
┌──────────────────────────── DEVICE PLANE (simulator today, real DC later) ────────────────────────────┐
│                                                                                                       │
│   SNMP agents        gNMI server        BACnet/IP + MS-TP        Redfish BMCs        Modbus/TCP        │
│   udp/161            tcp/57400          udp/47808                tcp/8443            tcp/502           │
│   (wildcard,         (target in         (device instance,        (EventService       (unit-id via      │
│    community=IP)      path prefix)       MS/TP via router)        subscriptions)      gateway)         │
│        │                  │                    │                      │                   │           │
│        └────── SNMP traps udp/162 ─────────────┴──────────────────────┴───────────────────┘           │
└───────────────────────────────────────────────┬───────────────────────────────────────────────────────┘
                                                │
┌───────────────────────────────────────────────▼───────────────────────────────────────────────────────┐
│                                     GO COLLECTOR  (1..N instances)                                     │
│                                                                                                        │
│   ┌── Inbound (push) ─────────────────┐   ┌── Outbound (poll) ────────────────────────────────────┐    │
│   │  SNMP Trap Receiver   udp/162     │   │  Scheduler (time wheel, jittered, per-endpoint)       │    │
│   │  Redfish Event Rcvr   tcp/9443    │   │  Worker pool + per-protocol semaphores                │    │
│   │  BACnet COV Rcvr      udp/47809   │   │  SNMP · gNMI · BACnet · Redfish · Modbus adapters     │    │
│   │  (gNMI STREAM lives in the poll   │   │  Session/connection pools, retry, timeout, backoff    │    │
│   │   plane — it is a long-lived job) │   │                                                       │    │
│   └───────────────┬───────────────────┘   └───────────────────────┬───────────────────────────────┘    │
│                   └───────────────┬───────────────────────────────┘                                    │
│                                   ▼                                                                    │
│              Normalizer  →  Validator  →  canonical Telemetry / Event / EndpointState                  │
│                                   │                                                                    │
│              Assignment client ◀──┴── (GET /api/v1/collector/assignments, ETag, 30 s)                  │
│              Health tracker (per endpoint: consecutive failures, last ok, last error)                  │
└───────────────────────────────────┬────────────────────────────────────────────────────────────────────┘
                                    │  publish (protobuf or msgpack, batched)
                                    ▼
                     ┌───────────────────────────────────────────┐
                     │              REDIS STREAMS                │
                     │   telemetry.v1     events.v1              │
                     │   endpointstate.v1 collectorhb.v1         │
                     │   (consumer group: dcim-ingest)           │
                     └───────────────────┬───────────────────────┘
                                         │
┌────────────────────────────────────────▼───────────────────────────────────────────────────────────────┐
│                          DCIM BACKEND (one codebase, three process roles)                              │
│                                                                                                        │
│   ┌── ingest worker (N) ────────────────┐  ┌── api (uvicorn, N) ──────┐  ┌── task worker (arq) ─────┐   │
│   │ consume → enrich (inventory cache)  │  │ REST  /api/v1/...        │  │ rollups, capacity calc,  │   │
│   │        → derive rates (counters)    │  │ WebSocket /ws            │  │ forecasting, seed import,│   │
│   │        → batch COPY  → Timescale    │  │ read-only + CRUD         │  │ discovery sweeps,        │   │
│   │        → upsert      → device_state │  │ never polls devices      │  │ report generation        │   │
│   │        → rule engine → alarms/events│  │ never blocks on ingest   │  │                          │   │
│   │        → publish     → Redis pubsub │  └──────────┬───────────────┘  └──────────────────────────┘   │
│   └─────────────────────────────────────┘             │                                                │
└──────────────────┬────────────────────────────────────┼────────────────────────────────────────────────┘
                   │                                    │
     ┌─────────────▼──────────────┐        ┌────────────▼─────────────┐
     │  PostgreSQL 16             │        │  Redis                   │
     │   + TimescaleDB extension  │        │   pub/sub  (WS fan-out)  │
     │  inventory · state · alarms│        │   cache    (inventory)   │
     │  hypertables · CAGGs       │        │   streams  (ingest bus)  │
     └────────────────────────────┘        │   locks    (leader elect)│
                                           └──────────────────────────┘
                   │
                   ▼
        ┌────────────────────────────────────────────┐
        │  REACT + TYPESCRIPT + VITE                 │
        │  REST for queries/CRUD, WS for live deltas │
        └────────────────────────────────────────────┘
```

---

## 2. Component responsibilities (hard boundaries)

| Component | Owns | Must never |
|---|---|---|
| **Go collector** | Wire protocols, sessions, retries, OID/path/object → canonical metric mapping, endpoint communication health, publishing to the stream | Touch the database. Know what a "rack" is. Evaluate business thresholds. |
| **Ingest worker** | The *only* writer to PostgreSQL/Timescale. Enrichment, rate derivation, current-state upsert, rule evaluation, alarm lifecycle, pub/sub emission | Serve HTTP. Talk to devices. |
| **API (FastAPI)** | REST + WebSocket, authz, validation, read models, CRUD on inventory, alarm ack/clear actions | Poll devices. Perform long analytics inline. Write telemetry rows. |
| **Task worker (arq)** | Scheduled rollups, capacity/forecast math, seed import, discovery sweeps, retention chores | Be on the request path. |
| **React** | Presentation, local UI state, chart rendering | Compute PUE, capacity, or any derived KPI. Poll REST in a loop for live data. |

Note the split of "device health": the **collector** decides whether an endpoint
is reachable (it is the only thing that knows a poll timed out). The **ingest
worker / rule engine** decides whether a *value* is out of range. Both produce
alarms, through the same alarm lifecycle.

---

## 3. Data flow, end to end

### 3.1 Polled telemetry

```
scheduler fires endpoint job
  → adapter.Collect(ctx, endpoint)              [protocol-specific]
  → []Telemetry with canonical metric keys      [normalize]
  → validate (range, unit, NaN, monotonicity)
  → batch (up to 500 samples / 200 ms)
  → XADD telemetry.v1
      ↓
  ingest worker XREADGROUP
  → enrich: device → rack → row → room → dc, vendor, model, device_type
  → counters: derive rate vs last value, drop on discontinuity
  → COPY into telemetry hypertable
  → UPSERT device_state (last_seen, last value per hot metric)
  → rule engine evaluate (dwell + hysteresis)
  → alarm raise/update/clear
  → PUBLISH dcim:telemetry / dcim:alarm
      ↓
  API WS session filters by subscription → browser
```

### 3.2 SNMP trap

```
device → udp/162 → trap receiver
  → decode PDU, extract varbinds + source IP
  → resolve source IP → endpoint → device (cache, miss ⇒ "unknown source" event)
  → vendor OID table → canonical event_type + severity + is_clear
  → Event{} → XADD events.v1
      ↓
  ingest worker
  → if is_clear: clear alarm with matching key
  → else: raise/update alarm with key (device, alarm_type, instance)
  → always: persist event row (the raw record survives regardless)
  → PUBLISH dcim:alarm / dcim:event
```

Traps are **never** used as a telemetry source for values. A trap tells you a
state changed; the value comes from the next poll. Mixing the two produces
sawtooth charts.

### 3.3 Redfish event

```
BMC → HTTPS POST → collector event receiver
  → verify subscription context + source
  → map MessageId (e.g. "Alert.1.0.TemperatureAbove") → canonical event_type
  → Event{} → events.v1   (identical downstream path to a trap)
```

Same canonical `Event` type as a trap. That is the whole point: the alarm engine
never learns that Redfish exists.

### 3.4 Inventory change (fleet lifecycle)

```
operator/simulator commissions a rack
  → seed importer (task worker) re-reads GET /api/topology/export
  → diff against inventory → insert devices/endpoints/connections, mark removed
  → bump assignments ETag + PUBLISH dcim:assignments
      ↓
  collector assignment client wakes, GETs assignments, diffs
  → starts jobs for new endpoints, stops jobs for removed ones
  → first poll lands within one interval
```

---

## 4. Deployment topology

**Development / demo (single host, alongside the simulator):**

```
docker compose:
  postgres (timescaledb-ha image)   :5432
  redis                             :6379
  dcim-api      (uvicorn)           :8000
  dcim-ingest   (1 replica)
  dcim-worker   (arq, 1 replica)
  dcim-collector(1 replica, host network — needs udp/162, udp/47808/47809)
  dcim-ui       (vite dev / nginx)  :5173 / :80
```

The collector needs **host networking** (or explicit UDP port mapping) because
it binds udp/162 for traps and must source BACnet from a routable address.

**Production shape (later, real DC):**

- Collector: one instance per site/zone, sharded by `collector_id`, placed on the
  management network. Collectors are stateless apart from their in-memory counter
  baselines.
- Redis: single instance is fine to start; Sentinel or a managed Redis when the
  stream becomes the only buffer between planes.
- Postgres/Timescale: one primary + one streaming replica for read-only
  analytics. Timescale multi-node is not needed at this scale.
- API: 2+ uvicorn workers behind nginx; WebSocket fan-out already works because
  it goes through Redis pub/sub.

---

## 5. Failure behaviour (design intent)

| Failure | Behaviour |
|---|---|
| Database down | Ingest worker stops acking; Redis stream grows to `MAXLEN`; collector keeps polling and publishing. On recovery the backlog drains. Nothing is lost until the cap is hit. |
| Redis down | Collector buffers in memory to a bounded queue, then sheds oldest telemetry (keeps events — events are rarer and more valuable). Emits a `collector_degraded` state. |
| Collector down | `collector_instance` heartbeat goes stale → backend raises a platform alarm and marks that shard's endpoints `UNKNOWN` (not `OFFLINE` — you do not know). This distinction matters and is a classic NMS bug. |
| One device unreachable | Endpoint health degrades after N failures → `endpointstate.v1` → device state `OFFLINE` → alarm, suppressed if its parent (OOB switch, gateway, router) is also down. |
| Ingest worker down | Consumer group pending entries are reclaimed by another worker via `XAUTOCLAIM` after idle timeout. Run at least 2. |
| Duplicate delivery | Everything downstream is idempotent: telemetry rows are keyed `(ts, device_id, metric_id, instance)`; alarm operations key on the alarm key. At-least-once is therefore safe. |

---

## 6. Technology decisions and the reason for each

| Decision | Choice | Reason |
|---|---|---|
| Collector language | Go | Concurrency, static binary, no GIL. Cost: BACnet library ecosystem is weak — see `08-protocol-adapters.md`. |
| Backend | FastAPI + SQLAlchemy 2.0 async + asyncpg + Pydantic v2 | As specified; async all the way to the driver or the async is decorative. |
| Bus | Redis Streams | No new infrastructure, consumer groups, replay, capped. Swap to NATS JetStream if multi-consumer/multi-site arrives. |
| Serialisation on the bus | **Protobuf** (`contracts/telemetry/v1/*.proto`) | Cross-language, versioned, generated types on both sides. msgpack is acceptable if you would rather not run `protoc`; JSON is not (10× the bytes and no schema). |
| Time-series | TimescaleDB hypertable + CAGGs | SQL joins to inventory; compression; retention policy in the database rather than in cron. |
| Current state | Plain Postgres table | Small, hot, updated in place. |
| Task queue | arq | Redis-based, async-native, far less ceremony than Celery. |
| Frontend data | TanStack Query (REST) + a single WS client feeding a store | REST is the source of truth on mount; WS applies deltas. Never the reverse. |
| Charts | uPlot or ECharts | Recharts will not render 10k points smoothly; these will. |

---

## 7. Explicitly rejected options

| Rejected | Why |
|---|---|
| Kafka | Operational weight with no benefit at 900 samples/s. |
| Prometheus as the store | Pull model fights the collector design; labels are not an inventory; joins to racks/rooms are not possible. |
| InfluxDB | Timescale keeps everything in one SQL engine with the inventory. One fewer datastore. |
| One microservice per protocol | Adapters share the scheduler, the health tracker and the publisher. Splitting them multiplies deployment cost and buys nothing. |
| Collector writing directly to the DB | Finding A1. |
| gRPC from collector to backend instead of a stream | Loses the buffer. If the backend is down, the collector is stuck. A stream is a buffer *and* a contract. |
| Server-Sent Events instead of WebSocket | Fine for one-way, but the client needs to send subscription changes. WS is the right shape here. |
