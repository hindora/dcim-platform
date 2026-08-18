# 09 — FastAPI Backend Design

One codebase, three process roles: **api**, **ingest**, **worker**. They share
models, repositories and services; they differ only in entrypoint.

---

## 1. Layering

```
api/v1/*.py        HTTP concerns only: routing, auth dependency, request/response schemas
   │  (calls)
services/*.py      business logic. No SQL. No FastAPI imports. Pure, testable.
   │  (calls)
repositories/*.py  ALL SQL. Returns ORM objects or plain dicts, never Pydantic models.
   │
models/*.py        SQLAlchemy ORM
```

Two rules that keep this honest, both enforced by an import-linter check in CI:

- `services/` may not import `fastapi` or `sqlalchemy`.
- `api/` may not import `sqlalchemy` except for the session dependency type.

A route handler that is longer than ~15 lines is a smell; it should be validating
input, calling one service method, and shaping the response.

---

## 2. Ingest worker

The only writer to the database. Run at least two replicas.

```python
async def run(worker_id: str) -> None:
    group, stream = "dcim-ingest", "telemetry.v1"
    await ensure_group(stream, group)
    while not shutdown.is_set():
        # 1. reclaim anything a dead worker left pending
        await xautoclaim(stream, group, worker_id, min_idle=60_000)
        # 2. read a batch
        entries = await redis.xreadgroup(group, worker_id, {stream: ">"},
                                         count=200, block=1000)
        if not entries:
            continue
        batches = [TelemetryBatch.FromString(e[b"p"]) for _, e in entries]
        samples = [s for b in batches for s in b.samples]

        async with uow() as tx:                     # one transaction for the whole batch
            enriched = await enrich(samples, tx)    # inventory cache lookup
            derived  = rates.derive(enriched)       # counters → rates, wrap/reset handling
            await writer.copy_samples(tx, enriched + derived)
            await writer.upsert_device_state(tx, enriched + derived)
            actions = await alarm_engine.evaluate(enriched + derived, tx)
        await fanout.publish(enriched, actions)     # AFTER commit
        await redis.xack(stream, group, *ids)       # AFTER publish
```

Order matters: **commit → publish → ack.** Publishing before commit can show the
UI a value that then rolls back. Acking before commit loses data on crash.
Acking after publish means at-least-once, which every sink is built to tolerate.

### 2.1 Enrichment

An in-process cache of the inventory, refreshed on a Redis pub/sub invalidation
message and every 60 s as a backstop:

```python
class InventoryCache:
    devices: dict[UUID, DeviceCtx]      # device_id → type, vendor, model, rack, row, room, dc
    endpoints: dict[UUID, EndpointCtx]
    interfaces: dict[tuple[UUID, str], InterfaceCtx]   # (device_id, ifIndex)
    metrics: dict[str, MetricDef]
```

At 664 devices this is a few hundred kilobytes. At 50,000 devices it is still
tens of megabytes — keep it in-process rather than round-tripping to Redis per
sample. A cache miss falls back to a database read and logs a warning, because a
persistent miss means the collector is polling something inventory does not know
about (which is itself worth an alarm).

### 2.2 Rate derivation

```python
def derive(sample: Telemetry, prev: PrevValue | None) -> Telemetry | None:
    if sample.value_type != COUNTER:
        return None
    if prev is None or sample.counter_reset:
        return None                                  # store the baseline, emit nothing
    dt = (sample.observed_at - prev.observed_at).total_seconds()
    if dt <= 0 or dt > MAX_GAP_S:                    # clock skew, or a gap too long to trust
        return None
    delta = sample.uint_value - prev.value
    if delta < 0:                                    # wrapped
        width = 1 << sample.counter_bits
        delta += width
        if delta > width // 2:                       # implausible: treat as a reset
            return None
    return Telemetry(metric=RATE_KEY[sample.metric], value=delta / dt, ...)
```

Three deliberate choices:

- A missing baseline emits **nothing**, not zero. Zero is a value and it lies.
- `MAX_GAP_S` (default `4 × interval`) prevents a 6-hour outage from producing a
  meaningless "average over 6 hours" data point that then dominates a chart.
- The "wrapped more than half the counter width" heuristic catches an agent
  restart that a `sysUpTime` check missed.

Baselines live in Redis (`HSET dcim:ctr:{endpoint} {metric}:{instance}`) with a
TTL of `10 × interval`, so a worker restart does not lose them and a
decommissioned endpoint expires by itself.

### 2.3 Writer

```python
async def copy_samples(tx, samples):
    records = [(s.observed_at, s.device_id, s.metric_id, s.instance, s.value, s.quality)
               for s in samples]
    await tx.conn.copy_records_to_table(
        "telemetry_sample",
        records=records,
        columns=["ts","device_id","metric_id","instance","value","quality"])
```

`COPY` cannot express `ON CONFLICT`. Two workable approaches:

1. Copy into a per-transaction `TEMP` table, then
   `INSERT ... SELECT ... ON CONFLICT DO NOTHING`. Costs one extra pass but is
   exactly correct.
2. Accept that duplicates are near-impossible in practice (they require a
   redelivery of an already-committed batch) and let the PK raise, retrying the
   batch row-by-row on conflict.

Take option 1. It is ~15 % slower at these volumes and removes an entire class of
incident.

`device_state` upsert writes only registry metrics flagged `hot: true`, guarded
so a late duplicate cannot overwrite newer state:

```sql
INSERT INTO device_state (device_id, power_w, inlet_temp_c, metrics, last_seen, updated_at)
VALUES (...)
ON CONFLICT (device_id) DO UPDATE SET
  power_w      = COALESCE(EXCLUDED.power_w, device_state.power_w),
  inlet_temp_c = COALESCE(EXCLUDED.inlet_temp_c, device_state.inlet_temp_c),
  metrics      = device_state.metrics || EXCLUDED.metrics,
  last_seen    = GREATEST(device_state.last_seen, EXCLUDED.last_seen),
  updated_at   = now()
WHERE device_state.updated_at <= EXCLUDED.updated_at;
```

---

## 3. Alarm engine (finding A6)

### 3.1 Model

```python
@dataclass(frozen=True)
class AlarmKey:
    device_id: UUID
    alarm_type: str
    instance: str = ""

@dataclass
class Candidate:
    key: AlarmKey
    severity: Severity
    message: str
    metric_key: str | None
    value: float | None
    threshold: float | None
    source: str            # threshold | trap | redfish_event | bacnet_cov | comm | staleness
    observed_at: datetime
```

Every path — thresholds, traps, Redfish events, BACnet COV, communication
failure, stale telemetry — produces `Candidate` objects, and a **single**
lifecycle function applies them. That is what stops the five sources from each
growing their own half-correct alarm handling.

### 3.2 Dwell and hysteresis

State per `(rule, key)` in Redis:

```python
class DwellState:
    breach_count: int
    clear_count: int
    first_breach_at: datetime | None
```

```python
def evaluate(rule, sample, st) -> Candidate | ClearSignal | None:
    breached = compare(sample.value, rule.operator, rule.threshold)
    clear_at = rule.clear_threshold if rule.clear_threshold is not None else rule.threshold

    if breached:
        st.clear_count = 0
        st.breach_count += 1
        st.first_breach_at = st.first_breach_at or sample.observed_at
        dwell_ok = st.breach_count >= rule.dwell_samples and (
            rule.dwell_seconds is None
            or (sample.observed_at - st.first_breach_at).total_seconds() >= rule.dwell_seconds)
        return Candidate(...) if dwell_ok else None

    if not compare(sample.value, rule.operator, clear_at):     # inside the deadband
        st.breach_count = 0
        st.clear_count += 1
        if st.clear_count >= rule.clear_dwell_samples:
            st.first_breach_at = None
            return ClearSignal(...)
    return None            # between clear_threshold and threshold: hold current state
```

The third branch is the whole point of hysteresis and is the part usually
omitted: a value between the clear threshold and the raise threshold changes
nothing. Without it, a metric oscillating around 80 raises and clears on every
sample.

### 3.3 Lifecycle

```python
async def apply(c: Candidate, tx) -> AlarmAction:
    existing = await repo.get_active(c.key, tx)
    if existing is None:
        alarm = await repo.insert(c, state="ACTIVE", first_seen=c.observed_at,
                                  last_seen=c.observed_at, occurrence_count=1)
        await repo.history(alarm, "raised")
        return AlarmAction("alarm_created", alarm)

    if existing.severity != c.severity:
        await repo.update(existing, prev_severity=existing.severity, severity=c.severity,
                          last_seen=c.observed_at,
                          occurrence_count=existing.occurrence_count + 1)
        await repo.history(existing,
                           "escalated" if c.severity > existing.severity else "deescalated")
        return AlarmAction("alarm_updated", existing)

    await repo.touch(existing, last_seen=c.observed_at)   # no WS event: nothing changed
    return AlarmAction.none()
```

Clearing:

```python
async def clear(key: AlarmKey, by: str, at: datetime, tx) -> AlarmAction:
    alarm = await repo.get_active(key, tx)
    if alarm is None:
        return AlarmAction.none()        # a clear with no raise is normal after a restart
    await repo.update(alarm, state="CLEARED", cleared_at=at, cleared_by=by)
    await repo.history(alarm, "cleared")
    await symptoms.release(alarm)        # un-suppress anything this was root cause for
    return AlarmAction("alarm_cleared", alarm)
```

**An acknowledged alarm that clears goes to `CLEARED`, not back to `ACTIVE`.**
And an alarm that re-raises after clearing is a **new row** with a new
`first_seen`, not a resurrection — otherwise MTTR statistics become meaningless.

### 3.4 Communication and staleness alarms

Two sources the proposal lists but does not place:

- **Communication.** Driven by `EndpointState` messages, not by a rule
  evaluation. `OFFLINE` → raise `endpoint_unreachable` (severity from the device
  type: a chiller offline is MAJOR, a lab server is MINOR). `ONLINE` → clear.
  The collector already applied the debounce, so the engine does not re-debounce.
- **Staleness.** A periodic task (every 60 s) that finds device_state rows whose
  hot metric timestamps are older than `metric.stale_after_s` while the endpoint
  is `ONLINE`. That combination — reachable but not reporting — is a real and
  distinct fault (an agent wedged, a BACnet point removed) and is invisible to
  both of the other paths.

### 3.5 Dependency suppression / correlation

```python
async def correlate(new_alarm, tx) -> None:
    if new_alarm.alarm_type not in SUPPRESSIBLE:      # unreachable, no_data, comm_fail
        return
    for layer in ("management", "fieldbus", "power"):
        for parent in await topo.upstream(new_alarm.device_id, layer, max_hops=2, tx=tx):
            root = await repo.get_active_any(parent.device_id,
                                             ROOT_TYPES[layer], tx)
            if root:
                await repo.update(new_alarm, is_symptom=True, root_cause_alarm_id=root.id)
                await repo.history(new_alarm, "suppressed",
                                   detail={"root": str(root.id), "layer": layer})
                return
```

Concrete cases this handles in this datacenter:

| Root | Layer | Symptoms suppressed |
|---|---|---|
| OOB switch `OOBM1-DC1-NR` unreachable | management | every device whose mgmt interface lands on it |
| Moxa gateway unreachable | fieldbus | the 6 RTU transmitters behind it |
| Loytec BACnet router unreachable | fieldbus | the MS/TP valves and pump cards behind it |
| PDU tripped | power | cord-fed servers in that rack — **but only if the B-side feed is also gone**; check `redundancy_side` before suppressing |
| Chiller down with no standby | cooling | downstream CRAH high-return-air alarms |

That parenthetical is the difference between a correlation engine and a
correlation liability: suppressing a server's alarm because its A feed failed,
when its B feed is fine, hides a real single-feed condition.

The UI shows an incident as one root row with a symptom count, and lets the
operator expand it. Never delete symptoms — suppression is a display and
notification decision, not a data decision.

### 3.6 Default rule set (starting point)

| alarm_type | metric | raise | clear | dwell | severity |
|---|---|---|---|---|---|
| `cpu_temp_high` | `cpu_temperature` | > 80 | < 75 | 3 | WARNING |
| `cpu_temp_critical` | `cpu_temperature` | > 90 | < 85 | 2 | CRITICAL |
| `inlet_temp_high` | `inlet_temperature` | > 27 | < 25 | 3 | WARNING |
| `inlet_temp_critical` | `inlet_temperature` | > 32 | < 30 | 2 | CRITICAL |
| `humidity_out_of_band` | `relative_humidity` | <20 or >70 | 25–65 | 5 | WARNING |
| `pdu_load_high` | `pdu_load_pct` | > 80 | < 75 | 3 | WARNING |
| `pdu_load_critical` | `pdu_load_pct` | > 90 | < 85 | 2 | CRITICAL |
| `ups_on_battery` | `ups_on_battery_state` | true | false | 1 | CRITICAL |
| `ups_battery_low` | `ups_battery_runtime` | < 10 min | > 15 | 1 | CRITICAL |
| `ups_load_high` | `ups_load_pct` | > 80 | < 75 | 3 | MAJOR |
| `chws_high` | `chws_temperature` | > setpoint + 2 | +1 | 5 | MAJOR |
| `cdu_leak` | `leak_alarm` | true | false | 1 | CRITICAL |
| `crah_airflow_loss` | `airflow_loss_alarm` | true | false | 1 | MAJOR |
| `generator_fuel_low` | `generator_fuel_level` | < 30 | > 35 | 3 | WARNING |
| `if_down` | `if_oper_state` | false | true | 2 | MAJOR |
| `endpoint_unreachable` | — | comm | comm | — | by device type |
| `telemetry_stale` | — | staleness | — | — | WARNING |

The ASHRAE-derived inlet thresholds (27 °C recommended upper, 32 °C allowable for
A2) are the defensible defaults; make them per-room overridable, because a
liquid-cooled hall and an air-cooled hall have different answers.

---

## 4. Topology service

```python
class TopologyService:
    async def graph(self, layer: Layer, scope: Scope) -> Graph
    async def upstream(self, device_id: UUID, layer: Layer, max_hops: int) -> list[Node]
    async def downstream(self, device_id: UUID, layer: Layer, max_hops: int) -> list[Node]
    async def path(self, src: UUID, dst: UUID, layer: Layer) -> list[Edge] | None
    async def impact(self, device_id: UUID) -> ImpactReport   # multi-layer blast radius
```

Traversal is a recursive CTE over `connection`, filtered by layer:

```sql
WITH RECURSIVE up AS (
    SELECT c.a_device_id AS device_id, 1 AS hops, c.redundancy_side
    FROM connection c
    WHERE c.b_device_id = :device_id AND c.layer = :layer AND c.admin_state = 'enabled'
  UNION ALL
    SELECT c.a_device_id, up.hops + 1, c.redundancy_side
    FROM connection c JOIN up ON c.b_device_id = up.device_id
    WHERE c.layer = :layer AND up.hops < :max_hops
)
SELECT DISTINCT device_id, min(hops) AS hops, array_agg(DISTINCT redundancy_side) AS sides
FROM up GROUP BY device_id;
```

Cache the whole graph per layer in Redis (it changes only on inventory change,
and at 2566 edges it is small). Invalidate on the same pub/sub message that
invalidates the inventory cache.

`impact()` is the analysis the correlation engine and the "what breaks if I take
this out" UI both need: for a candidate device, walk downstream on power,
cooling, management and production, and report which downstream loads lose their
**last remaining** feed versus which stay up on the other side.

---

## 5. Analytics

### 5.1 PUE (finding B6)

```python
async def pue(dc_id: UUID, start: datetime, end: datetime) -> PueResult:
    total_kwh = await repo.energy_delta(dc_id, "feed_energy", start, end)      # utility meter
    it_kwh    = await repo.energy_delta(dc_id, "pdu_energy",  start, end,
                                        device_types=IT_TYPES)
    if it_kwh <= 0:
        return PueResult(value=None, reason="no IT energy in window")
    return PueResult(value=total_kwh / it_kwh, total_kwh=total_kwh, it_kwh=it_kwh,
                     method="energy", level=2)
```

Points worth being explicit about:

- **Energy, not power.** `sum(power)/sum(power)` at a single instant is not PUE;
  it is a snapshot ratio that swings with the compressor duty cycle. The Green
  Grid definition is energy over a period. The simulator persists kWh, so use it.
- Report the **measurement level** (L1 UPS output / L2 PDU output / L3 IT
  equipment input). Comparing an L2 PUE with someone's L1 PUE is meaningless, and
  labelling it prevents the argument.
- Fall back to an instantaneous power ratio only when energy counters are
  unavailable, and mark the result `method="power"` so the UI can flag it.
- A partial interval (device commissioned mid-window) must not corrupt the sum —
  use `last(value) - first(value)` per device per window, then sum.

### 5.2 Capacity

Per rack / room / datacenter, four independent constraints — report all four,
because whichever binds first is the answer:

| Constraint | Metric |
|---|---|
| Power | `Σ power_draw` vs `rack.rated_power_kw` (and the feeding PDU/RPP/UPS rated capacity) |
| Cooling | rack heat load vs the CRAH/CDU capacity serving that row |
| Space | `Σ u_height` vs `rack.u_height`, with the largest contiguous free block |
| Connectivity | free ports on the serving ToR / OOB switch |

Use the **95th percentile of the last 30 days**, not the instantaneous value, for
"used" — sizing from a momentary peak strands capacity, and sizing from the mean
under-provisions. Report both p95 and peak.

### 5.3 Thermal

- Per-rack ΔT: mean inlet vs mean exhaust.
- Room ΔT: mean CRAH return vs mean CRAH supply.
- Hot-spot detection: inlet temperature > room p90 + 3 K sustained for 15 min.
- Cooling effectiveness: rack heat load (from power) vs measured airflow × ΔT.
- Correlate `crah_high_return_air_alarm` with rack inlets in that row — as the
  simulator's own comment notes, a high **return** means the hot aisle feeding
  the unit is too hot (a load symptom), while a high **supply/discharge** means
  the unit itself has failed. Those two must drive different runbooks.

### 5.4 Forecast

Keep it simple and honest: linear regression (or Holt-Winters where seasonality
is real) on the 1-hour continuous aggregate, projecting to the capacity limit,
reported with a confidence interval and an explicit "insufficient history" state
below 14 days. Anything fancier is unjustifiable until there is a year of data.

---

## 6. Seed importer

```python
async def import_from_simulator(base_url: str, token: str) -> ImportReport:
    topo = await http.get(f"{base_url}/api/topology/export")
    # 1. datacenters/rooms/rows/racks from device placement fields
    # 2. vendors, device_types, models
    # 3. devices keyed by external_id (the simulator's 8-char device id)
    # 4. interfaces, outlets, psus
    # 5. connections, one per edge, with layer taken from edge.layer
    # 6. endpoints derived from device_type + addressing (see 16-simulator-integration.md)
    # 7. mark devices absent from the export as decommissioned (never delete)
```

Idempotent, keyed on `device.external_id`. Re-runnable on every fleet change; a
scheduled 5-minute re-import is a perfectly good way to track lifecycle churn
until the simulator grows a change feed.

---

## 7. Configuration

```python
class Settings(BaseSettings):
    database_url: PostgresDsn
    redis_url: RedisDsn
    jwt_secret: SecretStr
    jwt_ttl_minutes: int = 60
    credential_key: SecretStr            # AES-GCM key for the credential table
    simulator_base_url: AnyHttpUrl | None = None
    simulator_token: SecretStr | None = None
    ingest_batch_size: int = 200
    ws_coalesce_ms: int = 1000
    model_config = SettingsConfigDict(env_prefix="DCIM_", env_file=".env")
```

No secret has a default. The app refuses to start if `jwt_secret` or
`credential_key` is missing, rather than generating one — a generated key that
changes on restart silently invalidates every stored credential.
