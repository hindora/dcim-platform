# 14 — Testing Strategy

The simulator is a real protocol implementation, not a mock. That is a large
advantage and it changes what the test pyramid should look like: **integration
tests are cheap here and should be the backbone**, with unit tests reserved for
the logic that is genuinely hard to reach through the wire (counter wraps, dwell
timers, correlation).

The instruction "do not create mock implementations where the simulator already
provides the real protocol interfaces" is correct and is followed throughout.

---

## 1. Layers

| Layer | Scope | Where it runs |
|---|---|---|
| Unit | pure functions: mapping transforms, rate derivation, dwell/hysteresis, correlation, unit conversion, capacity math | in-process, no I/O |
| Adapter integration | one adapter against the running simulator | CI with the simulator in a container |
| Pipeline integration | collector → Redis → ingest → Postgres | docker compose |
| API integration | FastAPI against a real Postgres+Timescale | testcontainers |
| End-to-end | simulator → collector → DB → API → WS → headless browser | docker compose |
| Load / soak | 5,000 synthetic endpoints, 24 h | on demand |
| Chaos | kill components, break the network, corrupt input | on demand |

---

## 2. Unit tests — the ones that actually earn their place

### 2.1 Counter handling (Go)

This is where NMS bugs live. Table-driven:

```go
func TestCounterDelta(t *testing.T) {
    cases := []struct {
        name       string
        prev, curr uint64
        bits       uint32
        dt         time.Duration
        reset      bool
        wantRate   float64
        wantEmit   bool
    }{
        {"normal",        1000, 2000, 64, time.Second, false, 1000, true},
        {"no baseline",      0, 2000, 64, time.Second, false,    0, false},  // emits nothing
        {"explicit reset", 9000, 100, 64, time.Second, true,     0, false},
        {"32-bit wrap", 4294967290, 100, 32, time.Second, false, 106, true},
        {"64-bit wrap", math.MaxUint64 - 10, 90, 64, time.Second, false, 101, true},
        {"implausible backwards", 9_000_000, 5, 64, time.Second, false, 0, false},
        {"gap too long", 1000, 2000, 64, 6 * time.Hour, false,   0, false},
        {"zero dt",       1000, 2000, 64, 0,            false,   0, false},
    }
    ...
}
```

Every one of those rows corresponds to a real production incident pattern.

### 2.2 Alarm dwell and hysteresis (Python)

```python
def test_hysteresis_holds_inside_deadband():
    rule = Rule(metric="cpu_temperature", op=">", threshold=80, clear_threshold=75,
                dwell_samples=3, clear_dwell_samples=2)
    eng, st = Engine(), DwellState()
    for v in (81, 82, 83):                       # 3 breaches → raise
        r = eng.evaluate(rule, s(v), st)
    assert isinstance(r, Candidate)

    for v in (78, 77, 76):                       # inside the deadband → no change
        assert eng.evaluate(rule, s(v), st) is None

    assert eng.evaluate(rule, s(74), st) is None      # 1st clear sample
    assert isinstance(eng.evaluate(rule, s(73), st), ClearSignal)   # 2nd → clear
```

Plus: flapping produces exactly one raise; a rule with `dwell_seconds` respects
wall time rather than sample count when the interval changes; an alarm that
clears and re-raises creates a new row with a new `first_seen`.

### 2.3 Correlation

```python
def test_pdu_failure_does_not_suppress_when_b_feed_is_healthy():
    # A-side PDU down, B-side up → the server's alarm is NOT a symptom
def test_pdu_failure_suppresses_when_both_feeds_gone():
def test_oob_switch_suppresses_downstream_unreachable():
def test_moxa_gateway_suppresses_its_rtu_slaves():
def test_clearing_root_releases_symptoms():
```

The first of those is the test that stops correlation from hiding a real
single-feed condition.

### 2.4 Mapping transforms

Every transform (`scale`, `offset`, `map`, `enum_to_bool`, `divide_by_oid`) with
a boundary case, plus a test that every metric key referenced by any mapping YAML
exists in the registry (this is the `mapcheck` CLI, run as a test too).

---

## 3. Adapter integration tests

Run against the live simulator. Each asserts the *contract*, not exact values —
values move.

```go
func TestSNMPAdapter_Server(t *testing.T) {
    sim := requireSimulator(t)                    // skips if not reachable
    ep := sim.EndpointFor(t, "server", "snmp", "os_agent")

    out, err := adapter.Poll(ctx, ep)
    require.NoError(t, err)

    assertHasMetric(t, out, "cpu_utilization", inRange(0, 100), "pct")
    assertHasMetric(t, out, "memory_utilization", inRange(0, 100), "pct")
    assertHasMetricWithInstances(t, out, "if_in_octets", atLeast(1))
    assertAllMetricsInRegistry(t, out)
    assertNoRawOIDsInMetricNames(t, out)
}
```

Per protocol, the cases that matter:

| Adapter | Must prove |
|---|---|
| SNMP | correct community per device type; **server has two endpoints** (OS on `ip_address`, BMC on `mgmt_ip`) and both work; a wrong community fails as a timeout, not a crash; `_NO_SNMP_TYPES` devices are not polled; a 664-endpoint sweep completes inside one interval |
| Trap | a trap injected via `POST /api/traps/send` arrives, resolves to the right device, maps to the right `event_type`; a vendor-rewritten OID (APC 318, Raritan 13742) maps to the same canonical type as the placeholder OID; a clear trap sets `is_clear` |
| gNMI | ONCE returns data for a target; STREAM delivers updates and a `sync_response`; killing and restarting the stream invalidates baselines; interface names normalise to the same instance as SNMP ifIndex |
| BACnet | ReadPropertyMultiple returns all AI+BI for a chiller; object names map to registry metrics; kW→W scaling applied; an MS/TP valve is readable **through** its router; a COV subscription delivers a notification |
| Redfish | session auth works and is reused; Thermal/Power parse; a subscription is created and `SubmitTestEvent` arrives at the receiver; subscription reconciliation removes a stale one; a null sensor reading becomes a `Miss`, not a zero |
| Modbus | a gateway's unit ids are read serially; encoding (float32_be vs int16_scaled) is honoured; a gateway timeout produces one failure, not six |

---

## 4. Pipeline integration

```python
async def test_telemetry_reaches_timescale():
    await collector.poll_once(endpoint_id)
    await wait_for(lambda: ingest.lag() == 0, timeout=10)
    rows = await db.fetch(
        "SELECT * FROM telemetry_sample WHERE device_id=$1 AND ts > now()-interval '1 min'",
        device_id)
    assert rows
    state = await db.fetchrow("SELECT * FROM device_state WHERE device_id=$1", device_id)
    assert state["status"] == "ONLINE"

async def test_duplicate_batch_is_idempotent():
    batch = make_batch()
    await publish(batch); await publish(batch)
    await wait_for_ingest()
    assert await count_samples(batch) == len(batch.samples)     # not 2×

async def test_ingest_survives_db_restart():
    await stop_postgres()
    await publish_many(1000)
    await start_postgres()
    await wait_for(lambda: ingest.lag() < 5, timeout=60)
    assert await count_samples() == 1000                        # nothing lost
```

That last test is the one that justifies the broker in finding A1. It should be
in CI, not a manual exercise.

---

## 5. End-to-end

The flow named in the requirements, made executable:

```python
async def test_temperature_change_reaches_the_browser():
    # 1. change the value in the simulator
    await sim.post(f"/api/devices/{sim_device}/override",
                   {"metric": "cpu_temp", "value": 91.0})
    # 2..5. collector polls, normalises, stores
    await wait_for(lambda: api_state(device)["metrics"]["cpu_temperature"]["v"] == 91.0,
                   timeout=60)
    # 6. alarm raised after dwell
    alarms = await api.get(f"/api/v1/devices/{device}/alarms?state=ACTIVE")
    assert any(a["alarm_type"] == "cpu_temp_critical" for a in alarms["items"])
    # 7. websocket delivered it
    assert await ws.received("alarm_created", timeout=10)
```

Other end-to-end scenarios, each mapping to a requirement:

| Scenario | Injected via | Asserts |
|---|---|---|
| SNMP trap → alarm → WS | `POST /api/traps/send` | event row, alarm raised, WS frame, clear trap clears it |
| BACnet alarm | `POST /api/devices/{id}/fault` on a chiller | BI flips, `chiller_high_pressure` alarm, plant view reflects it |
| gNMI stream update | simulator link break `POST /api/topology/links/break` | `if_oper_state` false, `if_down` alarm |
| Redfish failure | stop the Redfish controller | endpoint DEGRADED then OFFLINE after 3 failures, not before |
| Device unreachable | unbind the device IP | `endpoint_unreachable` alarm; other endpoints of the same device unaffected |
| Dependency suppression | break an OOB switch | one root alarm, N symptoms, alarm list shows 1 row by default |
| Fleet commission | `POST /api/fleet/provision-rack` | inventory grows, assignment version bumps, collector polls the new devices within one interval |
| Fleet decommission | remove devices | endpoints stop, no false unreachable alarms |
| PUE | run for an hour | PUE computed from energy, method=`energy`, plausible range 1.2–2.0 |

The last row is worth stating explicitly: a PUE outside 1.05–2.5 is almost
certainly a units or aggregation bug, and asserting the plausible range catches
it earlier than any unit test.

---

## 6. Load and soak

`tools/loadgen/` generates synthetic endpoints that answer real SNMP (a second
snmpsim instance) so the collector is exercised, not mocked.

| Test | Target | Pass criteria |
|---|---|---|
| Steady state | 5,000 endpoints @ 30 s | poll completion > 99.5 %, p95 poll < 1 s, ingest lag < 5 s, collector RSS stable over 24 h |
| Burst | 10,000 traps in 60 s | no drops beyond the configured rate limit, alarm latency p95 < 5 s |
| Query load | 50 concurrent dashboard clients | API p95 < 500 ms |
| WS fan-out | 200 clients × 20 topics | no slow-consumer disconnects, API CPU < 60 % |
| Soak | 72 h at nominal load | no fd growth, no goroutine growth, no memory growth, no ingest backlog |

Goroutine and fd counts must be **flat**, not merely bounded. A slow leak in the
counter-baseline map or a leaked gNMI stream shows up as a gentle slope and will
not be caught by a one-hour run.

---

## 7. Chaos

| Fault | Expected |
|---|---|
| Kill Postgres 5 min | collector keeps polling, stream grows, full recovery on restart, no data loss |
| Kill Redis 2 min | collector buffers then sheds telemetry (never events), `collector_degraded` alarm, recovery |
| Kill one ingest worker | the other reclaims pending entries via `XAUTOCLAIM`, no duplicates in the DB |
| Kill the collector | `collector_stale` alarm, endpoints go `UNKNOWN` (**not** `OFFLINE`) |
| Partition collector from devices | endpoints go `OFFLINE` with correlation grouping them, not 664 independent alarms |
| Clock skew +5 min on the collector | samples are not written into the future; skew detected and logged |
| Malformed BACnet APDU | one failed poll, logged, no panic, other endpoints unaffected |
| Simulator restart | endpoints recover; counter baselines invalidated; no throughput spike in the charts |

That last one is a specific, checkable assertion: after a simulator restart there
must be **no** interface-throughput spike. If there is, the `sysUpTime` reset
detection is broken.

---

## 8. CI

| Stage | Runs | Gate |
|---|---|---|
| lint | golangci-lint, ruff, mypy, eslint, tsc | any failure |
| unit | Go + Python + Vitest | any failure; coverage ≥ 80 % on `services/`, `alarms/`, `normalize/` |
| contract | `protoc` regeneration diff, `mapcheck`, `contract_diff.py` | any diff or breaking change without a version bump |
| integration | docker compose: simulator + Postgres + Redis + collector + backend | any failure |
| e2e | the scenarios in §5 | any failure |
| security | govulncheck, pip-audit, image scan | high severity |
| load | nightly, not per-PR | regression > 20 % on p95 |

Fixtures: the simulator's `topologies/dual_dc_enterprise.json` is the standard
integration fixture — 664 devices, two datacenters, all five layers. A smaller
single-rack topology is used for fast unit-adjacent tests.
