# 07 — Go Collector Design

The collector is a single static binary. It knows about protocols, endpoints and
metrics. It does **not** know about racks, rooms, alarms or thresholds.

---

## 1. Core interfaces

The proposal's `Collect(ctx, device) ([]Telemetry, error)` is close but wrong in
three ways: it is device-scoped rather than endpoint-scoped (finding A2), it
cannot express a push source (traps, Redfish events, gNMI streams), and it has no
place to report partial failure. This is the corrected set.

```go
package collector

// Adapter is a protocol implementation. One instance per protocol per process.
type Adapter interface {
    Protocol() Protocol

    // Init is called once at startup with the shared runtime.
    Init(ctx context.Context, rt *Runtime) error

    // Poll performs one collection cycle for one endpoint. It must respect
    // ctx deadlines and must be safe for concurrent calls on different endpoints.
    Poll(ctx context.Context, ep *Endpoint) (*PollOutcome, error)

    // Close releases sessions, pools and sockets.
    Close(ctx context.Context) error
}

// Subscriber is implemented by adapters that also have a push path:
// gNMI STREAM, Redfish EventService, BACnet COV.
type Subscriber interface {
    Adapter
    // Subscribe starts a long-lived subscription. It returns when ctx is
    // cancelled or the subscription is permanently broken. The manager restarts
    // it with backoff. Emissions go through Runtime.Sink.
    Subscribe(ctx context.Context, ep *Endpoint) error
}

// Receiver is implemented by adapters that own a listening socket shared by
// all endpoints: the SNMP trap receiver, the Redfish event HTTP receiver.
type Receiver interface {
    Protocol() Protocol
    Listen(ctx context.Context, rt *Runtime) error   // blocks until ctx done
}

// Discoverer is optional and used only by discovery sweeps.
type Discoverer interface {
    Discover(ctx context.Context, scope DiscoveryScope) ([]Candidate, error)
}
```

```go
// PollOutcome carries partial success explicitly. A poll that reads 18 of 20
// OIDs is a success with 2 misses, not a failure — and treating it as a failure
// is why some NMS mark healthy devices down.
type PollOutcome struct {
    Samples   []Telemetry
    Events    []Event        // adapters may synthesise events (e.g. state transitions)
    Misses    []Miss         // per-metric failures with a reason
    LatencyMs int
    Partial   bool
}

type Miss struct {
    Metric string
    Reason string    // "no_such_object" | "timeout" | "decode" | "not_supported"
}

type Endpoint struct {
    ID          string          // DCIM device_endpoint.id
    DeviceID    string
    DeviceType  string          // needed to select the mapping table
    Vendor      string
    Model       string
    Protocol    Protocol
    Role        string
    Address     netip.Addr
    Port        uint16
    Addressing  map[string]any  // community ref, bacnet instance, modbus unit id, gnmi target
    Via         *Endpoint       // gateway/router, resolved by the assignment client
    Credential  Credential
    Profile     PollProfile
    Enabled     bool
}
```

```go
// Runtime is what every adapter is given: everything shared, nothing global.
type Runtime struct {
    Sink     Sink                 // publish telemetry/events/state
    Mappings *mapping.Registry    // compiled protocol → metric tables
    Metrics  *obs.Metrics         // Prometheus collectors
    Log      *slog.Logger
    Resolver *EndpointResolver    // source IP → endpoint, for receivers
    Clock    Clock                // injectable for tests
}

type Sink interface {
    Telemetry(context.Context, []Telemetry) error
    Events(context.Context, []Event) error
    EndpointState(context.Context, EndpointState) error
}
```

**Why `Runtime` and not package-level globals.** Every adapter is then testable
with a fake sink and a fixed clock, which is what makes the counter/wrap tests in
`14-testing-strategy.md` possible at all.

---

## 2. Process structure

```
main
 └─ app.Run(ctx)
     ├─ config.Load()                     process config only
     ├─ obs.Init()                        slog + prometheus + /health /ready
     ├─ publish.NewRedisSink()            batching, backpressure, shedding
     ├─ mapping.Load("contracts/mappings")
     ├─ assign.NewClient()                ETag polling + pubsub nudge
     ├─ adapters: snmp, gnmi, bacnet, redfish, modbus
     ├─ receivers: snmpTrap(:162), redfishEvents(:9443), bacnetCOV(:47809)
     ├─ health.NewTracker()
     ├─ sched.New(...)                    time wheel
     └─ manager.Run(ctx)
          ├─ on assignment change → diff → start/stop jobs
          ├─ scheduler ticks → enqueue poll jobs
          ├─ worker pool executes jobs
          └─ heartbeat every 10 s
```

Graceful shutdown: `SIGTERM` → cancel root context → stop accepting new jobs →
wait up to 15 s for in-flight polls → **flush the publisher** → close adapters →
exit. Flushing before closing matters; dropping a full batch on shutdown is an
avoidable data gap.

---

## 3. Scheduler

Not one goroutine per endpoint (the proposal correctly forbids this, and at
10,000 endpoints it is 10,000 timers). Use a **hierarchical time wheel** with
1-second granularity.

```go
type Scheduler struct {
    wheel   [3600]*bucketList   // one hour of 1-second buckets, wrapping
    cursor  int
    mu      sync.Mutex
    jobs    map[string]*Job     // endpoint id → job
}

// Phase is deterministic per endpoint so restarts do not re-thunder,
// and evenly spread so 664 endpoints on a 30 s interval fire ~22 per second.
func phaseOffset(endpointID string, intervalS int) int {
    h := fnv.New32a()
    _, _ = h.Write([]byte(endpointID))
    return int(h.Sum32() % uint32(intervalS))
}
```

Each tick moves the cursor one second and enqueues that bucket's jobs. A job that
is still running when its next tick arrives is **skipped**, and a
`poll_skipped_total` counter is incremented — never queued twice. Overlapping
polls of the same endpoint corrupt counter deltas and are the most common cause
of "impossible" throughput spikes.

Long-lived subscriptions (gNMI STREAM, Redfish subscription maintenance) are not
scheduled jobs; they are supervised goroutines with exponential backoff
(1 s → 2 s → … → 60 s, ±20 % jitter) and a `restarts_total` counter.

---

## 4. Worker pool and concurrency limits

```go
type Pool struct {
    queue   chan *Job          // bounded; full queue ⇒ shed with a counter, never block the wheel
    workers int
    sem     map[Protocol]*semaphore.Weighted
    hostSem *hostLimiter       // per destination host, for Redfish/BACnet
}
```

Defaults, tuned to the constraints in finding B5:

| Protocol | Global concurrent | Per host | Timeout | Retries | Why |
|---|---:|---:|---|---:|---|
| SNMP | 256 | 4 | 3 s | 2 | UDP, cheap, high fan-out is fine |
| Redfish | 32 | 1 | 8 s | 1 | TLS handshake cost; BMCs are slow and serialise anyway |
| BACnet | 64 | 1 | 5 s | 2 | UDP with one outstanding request per device unless invoke IDs are tracked |
| Modbus | 64 | 1 | 3 s | 2 | TCP, one transaction at a time per unit; gateways serialise the RS-485 trunk |
| gNMI (poll) | 32 | 2 | 5 s | 1 | HTTP/2 multiplexes, so per-host can exceed 1 |

**Per host, not per endpoint** — a Moxa gateway fronting 6 RTU slaves is one
host, and hammering it with 6 concurrent reads produces timeouts that look like
device faults. The same applies to a Loytec BACnet router.

`queue` capacity = `workers × 8`. When it fills, shed the oldest *poll* job and
increment `poll_shed_total`; do not shed subscriptions or events.

---

## 5. Endpoint health state machine

```
                 ┌──────── success ────────┐
                 ▼                         │
  UNKNOWN ──▶ ONLINE ──fail──▶ DEGRADED ──fail×(N-1)──▶ OFFLINE
                 ▲                │                        │
                 └──── success ───┴──────── success ───────┘
```

```go
type Health struct {
    Status              CommStatus
    ConsecutiveFailures int
    LastSuccess         time.Time
    LastFailure         time.Time
    LastError           string
    LastErrorClass      string
}

const offlineThreshold = 3   // configurable per poll profile
```

Rules that matter:

1. **One failure is `DEGRADED`, not `OFFLINE`.** A single dropped UDP packet is
   normal. Declaring a device down on it produces an alarm storm every night.
2. `OFFLINE` requires `consecutive_failures >= offlineThreshold` **and**
   `now - last_success > 2 × interval`. Both conditions, because a long interval
   with two quick failures is not enough evidence.
3. Recovery is immediate on the first success. Asymmetric hysteresis is correct
   here: be slow to condemn, quick to forgive.
4. `EndpointState` is published **on transition only**.
5. Error classification is explicit — `timeout`, `auth`, `refused`,
   `unreachable`, `decode`, `protocol`. `auth` must never be lumped with
   `timeout`: an authentication failure means the credential is wrong, and
   retrying it 300 times an hour locks accounts on real hardware.
6. When the collector itself is degraded (publisher shedding, assignment stale),
   it must **not** mark endpoints offline. Distinguish "I cannot see it" from
   "it is not there" — this is the classic NMS bug that turns a management
   network blip into 600 false alarms.

---

## 6. Assignment client (finding A7)

```go
type Assignment struct {
    Version   uint32     `json:"version"`
    Endpoints []Endpoint `json:"endpoints"`
}

func (c *Client) loop(ctx context.Context) {
    tick := time.NewTicker(30 * time.Second)
    nudge := c.redis.Subscribe(ctx, "dcim:assignments")
    for {
        select {
        case <-ctx.Done():   return
        case <-tick.C:       c.refresh(ctx)
        case <-nudge.Channel(): c.refresh(ctx)     // sub-second reaction to fleet churn
        }
    }
}
```

`refresh` sends `If-None-Match` and treats `304` as a no-op. On `200`, diff by
endpoint id:

- **added** → register job, seed health `UNKNOWN`, first poll at a random offset
  within one interval (not immediately — 200 new endpoints from a hall
  commissioning would otherwise fire at once);
- **removed** → stop job, cancel subscription, publish a final `EndpointState`
  with `DISABLED`, drop counter baselines;
- **changed** (interval, credential, addressing) → restart the job;
- unchanged → leave running, and specifically **do not reset counter baselines**.

**Failure mode:** if the assignment API is unreachable, keep polling the last
known set and raise `assignment_stale`. Never fall back to "no endpoints" — that
would silently stop all collection.

---

## 7. Publisher

```go
type RedisSink struct {
    rdb      *redis.Client
    telemCh  chan Telemetry
    eventCh  chan Event
    maxBatch int            // 500
    maxDelay time.Duration  // 200ms
    ring     *ringBuffer    // bounded fallback, 50k samples
}
```

Behaviour:

1. Accumulate until `maxBatch` or `maxDelay`.
2. Marshal to protobuf, `XADD stream MAXLEN ~ N * <payload>`.
3. On error: 3 retries with backoff. Then push to `ring`. Then, if `ring` is
   full, drop the **oldest telemetry** and increment `publish_dropped_total`.
   Events are never dropped while telemetry remains droppable.
4. `Flush(ctx)` on shutdown.
5. Emit `publish_queue_depth`, `publish_batch_size`, `publish_latency_ms`,
   `publish_dropped_total` — these four are what tell you the pipeline is sick
   before the DCIM does.

---

## 8. Configuration (`configs/collector.yaml`)

Note what is **absent**: any device.

```yaml
collector:
  id: col-1                       # shard identity
  version_check_interval: 30s

dcim:
  base_url: http://dcim-api:8000
  token_env: DCIM_COLLECTOR_TOKEN   # never a literal token in this file
  assignment_path: /api/v1/collector/assignments

redis:
  url_env: DCIM_REDIS_URL
  streams:
    telemetry:     { name: telemetry.v1,     maxlen: 2000000 }
    events:        { name: events.v1,        maxlen: 500000 }
    endpointstate: { name: endpointstate.v1, maxlen: 200000 }
    heartbeat:     { name: collectorhb.v1,   maxlen: 10000 }

publisher:
  max_batch: 500
  max_delay: 200ms
  ring_capacity: 50000

workers:
  pool_size: 128
  queue_multiplier: 8

protocols:
  snmp:
    enabled: true
    max_concurrent: 256
    per_host: 4
    timeout: 3s
    retries: 2
    max_repetitions: 25          # GETBULK
  snmp_trap:
    enabled: true
    listen: "0.0.0.0:162"
    workers: 8
  gnmi:
    enabled: true
    max_concurrent: 32
    timeout: 5s
    stream_backoff: { initial: 1s, max: 60s, jitter: 0.2 }
  bacnet:
    enabled: true
    max_concurrent: 64
    per_host: 1
    timeout: 5s
    local_device_instance: 260001
    bind: "0.0.0.0:47809"        # NOT 47808 — see 08-protocol-adapters.md §4.1
    cov_lifetime: 300s
  redfish:
    enabled: true
    max_concurrent: 32
    per_host: 1
    timeout: 8s
    idle_conn_timeout: 90s
    events:
      enabled: true
      listen: "0.0.0.0:9443"
      public_url: https://collector-1.dcim.local:9443/redfish-events
      tls_cert_file: /etc/dcim/collector.crt
      tls_key_file:  /etc/dcim/collector.key
  modbus:
    enabled: true
    max_concurrent: 64
    per_host: 1
    timeout: 3s

observability:
  log_level: info
  log_format: json
  metrics_listen: "0.0.0.0:9100"
  health_listen:  "0.0.0.0:9101"

limits:
  max_open_files: 65536          # checked at boot; refuse to start if lower
```

---

## 9. Go implementation notes

**Libraries.**

| Purpose | Library | Note |
|---|---|---|
| SNMP poll + trap | `github.com/gosnmp/gosnmp` | Mature. Handles v2c and v3, GETBULK, and trap listening. |
| gNMI | `github.com/openconfig/gnmi` (proto) + `google.golang.org/grpc` | Use the official protos; a hand-rolled client is fine and small. |
| Redfish | `net/http` + `encoding/json` | Do **not** use a heavyweight Redfish SDK. The resource set is small and stable; a typed client over 8 URLs is less code and far more predictable. |
| Modbus | `github.com/simonvetter/modbus` or `github.com/goburrow/modbus` | Both fine; the former handles unit-id switching more cleanly for gateways. |
| BACnet | **no mature option** | See `08-protocol-adapters.md` §4 — this is the one real cost of choosing Go. |
| Redis | `github.com/redis/go-redis/v9` | |
| Logging | `log/slog` | stdlib since 1.21; no dependency needed. |
| Metrics | `github.com/prometheus/client_golang` | |
| Config | `github.com/knadh/koanf` or `spf13/viper` | koanf is lighter. |

**Idioms to hold to:**

- Every blocking call takes a `context.Context` with a deadline derived from the
  poll profile, not a hardcoded constant.
- No package-level mutable state. Everything hangs off `Runtime`.
- Errors wrap with `%w` and carry an error class via a typed sentinel
  (`errors.Is(err, ErrAuth)`), because the health tracker branches on class.
- `sync.Pool` for the per-poll sample slice — at 900 samples/s the allocation
  churn is real and shows up as GC pressure.
- Counter baselines live in a sharded map keyed by
  `(endpointID, metric, instance)` with an LRU cap, so a decommissioned endpoint
  cannot leak memory.
- Never `panic` in an adapter. A `recover` at the job boundary converts a panic
  into a failed poll and a logged stack, because one malformed BACnet APDU must
  not take down collection for 600 devices.

---

## 10. Health and readiness

```
GET /health   → 200 always if the process is alive (liveness)
GET /ready    → 200 only when:
                  - Redis reachable AND
                  - assignment fetched at least once AND
                  - all enabled adapters initialised AND
                  - trap/event listeners bound
              → 503 with a JSON body listing which check failed
```

`/ready` returning 503 must **not** stop collection — it only removes the
instance from a load balancer. Collection continues on the last known assignment.
