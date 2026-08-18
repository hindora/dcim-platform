# 05 — Canonical Telemetry Contract v1

This is the interface between the collection plane and the DCIM plane. It is a
**released artefact**, not an implementation detail. Once v1 ships, it changes
only by the rules in §6.

Location: `contracts/proto/dcim/telemetry/v1/`.

---

## 1. Why protobuf rather than JSON

The proposal's example payloads are JSON. JSON is the right thing to *document*
with and the wrong thing to put on the bus:

- ~10× the bytes for numeric telemetry.
- No schema, so a field typo is a runtime surprise on the far side.
- No generated types, so Go and Python drift.

Protobuf gives generated types on both sides, forward/backward compatibility
rules that are actually specified, and a compact wire form. If you would rather
not run `protoc`, **msgpack with a shared schema test** is an acceptable second
choice. Plain JSON is not.

The JSON forms shown below are the canonical *documentation* rendering and are
what the debug endpoints emit.

---

## 2. Messages

### 2.1 `common.proto`

```protobuf
syntax = "proto3";
package dcim.telemetry.v1;

enum Protocol {
  PROTOCOL_UNSPECIFIED = 0;
  PROTOCOL_SNMP        = 1;
  PROTOCOL_SNMP_TRAP   = 2;
  PROTOCOL_GNMI        = 3;
  PROTOCOL_BACNET      = 4;
  PROTOCOL_REDFISH     = 5;
  PROTOCOL_MODBUS      = 6;
  PROTOCOL_SFLOW       = 7;
}

enum ValueType {
  VALUE_TYPE_UNSPECIFIED = 0;
  VALUE_TYPE_GAUGE       = 1;  // instantaneous
  VALUE_TYPE_COUNTER     = 2;  // monotonic, rate derived downstream
  VALUE_TYPE_DELTA       = 3;  // already a difference over the interval
  VALUE_TYPE_BOOL        = 4;
  VALUE_TYPE_TEXT        = 5;
}

enum Quality {
  QUALITY_UNSPECIFIED = 0;
  QUALITY_GOOD        = 1;
  QUALITY_STALE       = 2;  // served from cache / device says value is old
  QUALITY_SUSPECT     = 3;  // out of declared valid range, or sensor fault flag set
  QUALITY_BAD         = 4;  // read failed but a placeholder is being reported
  QUALITY_NO_DATA     = 5;
}

enum Severity {
  SEVERITY_UNSPECIFIED = 0;
  SEVERITY_CLEAR       = 1;
  SEVERITY_INFO        = 2;
  SEVERITY_WARNING     = 3;
  SEVERITY_MINOR       = 4;
  SEVERITY_MAJOR       = 5;
  SEVERITY_CRITICAL    = 6;
}

enum CommStatus {
  COMM_STATUS_UNSPECIFIED = 0;
  COMM_STATUS_ONLINE      = 1;
  COMM_STATUS_DEGRADED    = 2;
  COMM_STATUS_OFFLINE     = 3;
  COMM_STATUS_UNKNOWN     = 4;
}
```

### 2.2 `telemetry.proto`

```protobuf
syntax = "proto3";
package dcim.telemetry.v1;
import "google/protobuf/timestamp.proto";
import "dcim/telemetry/v1/common.proto";

// One observation of one metric on one instance of one device.
message Telemetry {
  string   endpoint_id   = 1;   // DCIM device_endpoint.id (UUID) — authoritative identity
  string   device_id     = 2;   // DCIM device.id (UUID), carried for convenience
  string   metric        = 3;   // canonical metric key from the registry
  string   instance      = 4;   // "" for device-scoped; else ifIndex / sensor id / phase / object

  ValueType value_type   = 5;
  oneof value {
    double  double_value = 6;
    uint64  uint_value   = 7;   // counters — full 64-bit, no float precision loss
    bool    bool_value   = 8;
    string  text_value   = 9;
  }
  string   unit          = 10;  // from the registry; carried so the payload is self-describing

  google.protobuf.Timestamp observed_at  = 11;  // device/protocol time where available
  google.protobuf.Timestamp collected_at = 12;  // collector wall clock

  Protocol source_protocol = 13;
  Quality  quality         = 14;

  // Counter bookkeeping. Set only when value_type == COUNTER.
  bool     counter_reset   = 15;  // true ⇒ discard the delta across this sample
  uint32   counter_bits    = 16;  // 32 or 64, for wrap arithmetic

  map<string, string> metadata = 20;
    // protocol-specific provenance, for debugging only. Never business logic.
    // snmp    : {"oid":"1.3.6.1.2.1.2.2.1.10.1"}
    // bacnet  : {"object_type":"analog-input","object_instance":"1001"}
    // redfish : {"pointer":"/Thermal#/Temperatures/2"}
    // gnmi    : {"path":"/interfaces/interface[name=Et1]/state/counters/in-octets"}
    // modbus  : {"unit_id":"7","register":"40001"}
}

// A batch is what actually goes on the wire; per-sample framing is wasteful.
message TelemetryBatch {
  string collector_id = 1;
  repeated Telemetry samples = 2;
  google.protobuf.Timestamp sent_at = 3;
  uint32 schema_version = 4;   // = 1
}
```

**Design notes, each with a reason:**

- **`endpoint_id`, not `device_id`, is the identity.** A server has two SNMP
  endpoints; without the endpoint you cannot attribute a failure or a
  duplicate-metric collision. `device_id` is carried so the ingest worker's
  common path needs no join.
- **`uint64` for counters.** `double` loses integer precision above 2^53, which
  a busy 100 G interface reaches in weeks. This is a real bug in several NMS
  products.
- **`instance` is a string, not an int.** BACnet object instances, ifIndexes,
  phase letters and sensor names all have to fit.
- **Two timestamps.** Finding B1.
- **`metadata` is explicitly debug-only.** The rule "no protocol details in the
  business layer" is enforced by convention here; putting the OID in the payload
  is fine, *branching on it* in the backend is a review failure.

### 2.3 Events

```protobuf
// A discrete state-change notification: SNMP trap, Redfish event, BACnet COV.
message Event {
  string   endpoint_id = 1;
  string   device_id   = 2;     // "" if the source could not be resolved
  string   source_ip   = 3;     // always populated — the fallback identity

  string   event_type  = 4;     // canonical, e.g. "link_down", "ups_on_battery"
  string   instance    = 5;
  Severity severity    = 6;
  bool     is_clear    = 7;     // true ⇒ this clears the alarm with the same key
  string   message     = 8;

  google.protobuf.Timestamp observed_at  = 9;
  google.protobuf.Timestamp collected_at = 10;
  Protocol source_protocol = 11;

  map<string, string> varbinds = 12;  // trap varbinds / Redfish MessageArgs / COV properties
  string   raw_identifier      = 13;  // trap OID, Redfish MessageId, BACnet object ref
  string   dedup_key           = 14;  // collector-computed; ingest uses it for idempotency
}

message EventBatch {
  string collector_id = 1;
  repeated Event events = 2;
  google.protobuf.Timestamp sent_at = 3;
  uint32 schema_version = 4;
}
```

`is_clear` is the field that makes the simulator's paired clear traps
(`cpuNormal`, `temperatureNormal`, `upsUtilityRestored`, …) work end-to-end. The
collector sets it from the trap mapping table; the backend never parses an OID.

### 2.4 Endpoint state and collector heartbeat

```protobuf
message EndpointState {
  string   endpoint_id = 1;
  string   device_id   = 2;
  string   collector_id = 3;
  CommStatus status    = 4;
  google.protobuf.Timestamp last_success = 5;
  google.protobuf.Timestamp last_failure = 6;
  uint32   consecutive_failures = 7;
  string   last_error       = 8;
  string   last_error_class = 9;   // timeout|auth|refused|decode|unreachable|protocol
  uint32   latency_ms       = 10;
  google.protobuf.Timestamp changed_at = 11;
}

message CollectorHeartbeat {
  string collector_id = 1;
  string version      = 2;
  string hostname     = 3;
  google.protobuf.Timestamp started_at = 4;
  google.protobuf.Timestamp sent_at    = 5;
  uint32 endpoints_owned   = 6;
  uint32 endpoints_online  = 7;
  uint64 polls_total       = 8;
  uint64 polls_failed      = 9;
  uint64 traps_received    = 10;
  uint64 events_received   = 11;
  uint32 queue_depth       = 12;
  uint32 active_streams    = 13;
  uint32 assignment_version = 14;
}
```

`EndpointState` is published **on change only**, not every poll. That is what
keeps the events stream quiet enough to be interesting.

---

## 3. Documentation rendering (JSON)

```json
{
  "endpoint_id": "6d2f1c9e-...-a1",
  "device_id":   "fa03fbfd-...-77",
  "metric":      "cpu_temperature",
  "instance":    "cpu0",
  "value_type":  "GAUGE",
  "double_value": 67.5,
  "unit":        "C",
  "observed_at": "2026-08-18T10:00:00.000Z",
  "collected_at":"2026-08-18T10:00:00.184Z",
  "source_protocol": "REDFISH",
  "quality":     "GOOD",
  "metadata":    { "pointer": "/redfish/v1/Chassis/1/Thermal#/Temperatures/0" }
}
```

```json
{
  "endpoint_id": "0a1b...-e4",
  "device_id":   "8f77...-31",
  "metric":      "chws_temperature",
  "instance":    "",
  "value_type":  "GAUGE",
  "double_value": 11.8,
  "unit":        "C",
  "observed_at": "2026-08-18T10:00:00Z",
  "collected_at":"2026-08-18T10:00:00Z",
  "source_protocol": "BACNET",
  "quality":     "GOOD",
  "metadata":    { "object_type": "analog-input", "object_instance": "1" }
}
```

```json
{
  "endpoint_id": "3c9d...-b2",
  "source_ip":   "10.51.11.40",
  "event_type":  "pdu_overload",
  "instance":    "bank1",
  "severity":    "MAJOR",
  "is_clear":    false,
  "message":     "rPDU load exceeded high threshold on bank 1",
  "observed_at": "2026-08-18T10:30:02Z",
  "source_protocol": "SNMP_TRAP",
  "raw_identifier":  "1.3.6.1.4.1.318.0.276",
  "varbinds": { "1.3.6.1.4.1.318.1.1.12.2.3.1.1.2.1": "34" },
  "dedup_key": "3c9d...-b2|pdu_overload|bank1|1755512202"
}
```

---

## 4. Transport

| Stream | Message | Producer | Consumer group | MAXLEN |
|---|---|---|---|---|
| `telemetry.v1` | `TelemetryBatch` | collector | `dcim-ingest` | `~ 2000000` |
| `events.v1` | `EventBatch` | collector | `dcim-ingest` | `~ 500000` |
| `endpointstate.v1` | `EndpointState` | collector | `dcim-ingest` | `~ 200000` |
| `collectorhb.v1` | `CollectorHeartbeat` | collector | `dcim-ingest` | `~ 10000` |

Each Redis stream entry is a single field `p` holding the serialised batch.

**Batching policy in the collector publisher:**

- flush at 500 samples, or 200 ms, whichever comes first;
- one batch never mixes endpoints from different protocols (simplifies retry
  accounting and makes the metrics readable);
- on `XADD` failure: retry 3× with backoff, then buffer in a bounded in-memory
  ring (default 50k samples), then **shed oldest telemetry while preserving
  events** — events are rarer and carry more information per byte;
- publish a `collector_degraded` state whenever shedding is active.

**Ordering.** Redis streams are ordered per stream. The ingest worker must not
assume ordering *across* streams — an event may arrive before the telemetry
sample that caused it. The alarm engine is written to be order-independent
(it keys on alarm keys and compares timestamps), so this is safe.

---

## 5. Delivery semantics

At-least-once, with idempotency at every sink:

| Sink | Idempotency mechanism |
|---|---|
| `telemetry_sample` | PK `(device_id, metric_id, instance, ts)` + `ON CONFLICT DO NOTHING` |
| `device_state` | Upsert guarded by `updated_at < excluded.updated_at` — never let a late duplicate overwrite newer state |
| `alarm` | Unique partial index on the alarm key; raise is an upsert |
| `event` | `dedup_key` unique index over a 24 h window |

`XACK` only after the database transaction commits. Unacked entries older than
the idle timeout are reclaimed with `XAUTOCLAIM` by any worker in the group.

---

## 6. Versioning policy

The package name carries the version: `dcim.telemetry.v1`.

**Allowed in v1 (backward compatible):**
- add a new optional field with a new tag number;
- add a new enum value **only if consumers treat unknown values as
  `UNSPECIFIED`** — which both generated stacks do, and which the ingest worker
  must handle explicitly rather than crashing;
- add a new metric key to the registry (the registry is data, not schema);
- add a new stream.

**Requires v2 (breaking):**
- remove or renumber a field;
- change a field's type or meaning;
- change the unit of an existing metric key (add a new key instead —
  `chws_temperature_f` is a new metric, never a redefinition);
- change the identity semantics of `endpoint_id` / `instance`.

**Migration procedure for v2:** the collector publishes to `telemetry.v2` while
still publishing `telemetry.v1`; ingest consumes v2 when present; v1 is removed
one release later. Dual-publish is cheap and avoids a lockstep deploy.

`tools/contract_diff.py` runs in CI and fails the build on any breaking change
that has not bumped the package version.

---

## 7. What must never enter this contract

- Rack, room, row, datacenter — that is **enrichment**, and the collector does
  not know it. Putting placement in the payload means the collector needs an
  inventory copy, which is a second source of truth.
- Thresholds, severity for *metric* values, or alarm decisions — those are
  business logic. (Event severity is different: it comes from the vendor's own
  notification semantics, which the collector's mapping table does know.)
- Vendor, model, serial — inventory attributes. The exception is discovery
  payloads, which are a different message on a different path.
- Anything the backend would have to parse a string to use.
