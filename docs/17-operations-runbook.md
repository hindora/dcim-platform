# 17 — Operations runbook

For whoever is holding the pager. Symptom first, then what it means, then what
to do. Every command here has been run against the live deployment; where a
command has a trap in it, the trap is written down next to it rather than left
to be discovered at 3am.

---

## 1. The system in one page

Five processes and two containers. Nothing here is supervised — every process
was started by hand and **will not come back on its own**.

| Piece | What it does | If it dies |
|---|---|---|
| **collector** (Go) | Polls devices over SNMP/BACnet/Modbus/gNMI/Redfish, publishes to Redis | No new telemetry. Endpoints keep their last status; `collector_stale` raises after ~60–90 s |
| **ingest worker** (Python) | Drains Redis streams, writes telemetry, evaluates alarms, monitors the platform | Redis backs up to its cap and then sheds. No alarms evaluated. No platform self-monitoring |
| **API** (FastAPI) | Reads, the UI, collector assignments | UI and API dead. Collectors keep polling from their last assignment and buffer to Redis |
| **Postgres** (container) | Everything durable | Worker cannot write; the collector keeps polling and buffers. See §5.4 for the loss boundary |
| **Redis** (container) | Telemetry/event streams, counter baselines | Collector buffers, then sheds telemetry (never events). `collector_degraded` raises |
| **simulator** | The device plane under test | Every endpoint goes OFFLINE. This is not the DCIM failing |

The dependency that surprises people: **the collector does not talk to
Postgres**. It talks to the API (for assignments) and to Redis (to publish). A
database outage does not stop collection.

---

## 2. Where it runs, and how to restart it

Everything is in WSL (`Ubuntu`) except Postgres and Redis, which are Docker
Desktop containers on Windows.

```bash
# What should be running
wsl -d Ubuntu -- pgrep -af 'bin/collector|app.ingest.worker|uvicorn app.main'
docker ps --format '{{.Names}}\t{{.Status}}'      # dcim-postgres-1, dcim-redis-1
```

### 2.1 The trap: every process needs `deploy/.env`

All of these were started from a shell that had `deploy/.env` sourced. Relaunch
one without it and it either exits immediately on a missing setting, or — worse
— comes up looking healthy and does nothing useful. **The collector reads its
Redis URL from `DCIM_REDIS_URL` (`url_env:` in `collector-live.yaml`); the
fallback `url:` in that file carries no password.** A collector started without
the variable polls happily and fails every publish with `NOAUTH
Authentication required`. It looks alive on every dashboard and moves no data.

Use this preamble for **any** restart:

```bash
cd /home/hari/dcim-platform
set -a; . ./deploy/.env; set +a
export DCIM_DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER:-dcim}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB:-dcim}"
export DCIM_REDIS_URL="redis://:${REDIS_PASSWORD}@127.0.0.1:6379/0"
```

### 2.2 Restart commands

```bash
# collector
cd /home/hari/dcim-platform/collector
setsid nohup ./bin/collector --config /home/hari/collector-live.yaml \
    > /home/hari/collector.log 2>&1 < /dev/null &

# ingest worker
cd /home/hari/dcim-platform
setsid nohup ./backend/.venv/bin/python -m app.ingest.worker \
    > /home/hari/worker.log 2>&1 < /dev/null &

# API
cd /home/hari/dcim-platform
setsid nohup ./backend/.venv/bin/python -m uvicorn app.main:app \
    --host 0.0.0.0 --port 8000 --app-dir backend \
    > /home/hari/api.log 2>&1 < /dev/null &

# containers
docker start dcim-postgres-1 dcim-redis-1
```

**Always verify, never assume.** A restart that silently failed is the most
expensive state this system has, because everything downstream reads as "quiet":

```bash
wsl -d Ubuntu -- pgrep -af 'bin/collector|app.ingest.worker'
wsl -d Ubuntu -- bash -c "grep -c 'publish failed\|NOAUTH' /home/hari/collector.log"   # must be 0
```

---

## 3. The three numbers, and why they are three

Confusing these is the single most common way to misdiagnose this platform.

| Number | Where | Healthy | What it means |
|---|---|---|---|
| **Pipeline lag** (`dcim_ingest_lag_seconds`) | `/metrics`, `/api/v1/collector/health` | < 1 s | Collector publish → committed row. The pipeline's own latency |
| **Telemetry age** (`dcim_telemetry_age_seconds`) | same | up to one poll interval (120 s) | How old the newest sample is |
| **Collector heartbeat age** | `/api/v1/collector/health` | < 60 s | Whether the collector is alive at all |

Three rules that follow:

* **Do not alert on telemetry age at 60 s.** Power is polled every 120 s, so a
  perfectly healthy fleet routinely has a newest sample 100 s old. An alert
  there fires forever and gets switched off.
* **Do not judge recovery by telemetry age.** After any outage the worker
  replays a backlog, and buffered samples are written with the timestamps they
  were *collected* at. Telemetry age therefore stays stale for the whole
  replay while the pipeline runs flat out — measured at **1,285 rows/second
  with a reported "age" of three minutes**. Judge recovery by row growth:

  ```sql
  SELECT count(*) FROM telemetry_sample;   -- run twice, 30 s apart
  ```
* **A missing number is not zero.** If telemetry age is null, nothing has ever
  been written; that is `ingest_stalled`, not silence.

---

## 4. Alarm runbooks

Platform alarms appear in the same alarm list as device faults, with no device
attached. `GET /api/v1/collector/health` is the operator view.

### `collector_stale`
The collector has not checked in for 60 s. Expect it ~60–90 s after a death.

1. `wsl -d Ubuntu -- pgrep -af 'bin/collector'` — is it running?
2. If not, restart it (§2.2) and **verify publish failures are 0**.
3. If it *is* running, it is alive but not heartbeating: check
   `grep NOAUTH /home/hari/collector.log`. That is the missing-`DCIM_REDIS_URL`
   case from §2.1.

Endpoints will **not** go OFFLINE for this — the devices are fine, the
monitoring is not. See §8 for what they do instead.

### `collector_degraded`
Publish drops, a queue over 80% full, or the collector's own endpoint counts
disagreeing. Drops mean **data that no longer exists anywhere** — not delay.

1. Check Redis is up and reachable: `docker ps | grep redis`.
2. Check the log for `shedding telemetry`. If Redis was down, this is expected
   and self-clears once it returns.
3. If Redis is healthy and drops continue, the collector is producing faster
   than it can publish — that is a capacity problem, see §5.

### `ingest_lag_high`
Publish-to-commit exceeded 60 s (warning) or 300 s (critical).

1. Almost always a backlog draining after an outage. Check row growth (§3) —
   if rows are climbing fast, it is working; wait.
2. If rows are not climbing: is the worker alive? Is Postgres accepting
   connections?
3. **Do not read stream depth as a health signal.** `telemetry.v1` is a capped
   ring: `XLEN` reports 8000 during completely normal operation, because that
   is its maxlen, not because anything is wrong. It cannot distinguish a
   healthy steady state from an overflowing one.

   The signal that does distinguish them is the drop counter, and the
   collector's own log:

   ```bash
   curl -s http://127.0.0.1:9100/metrics | grep dcim_collector_publish_dropped_total
   wsl -d Ubuntu -- bash -c "grep -c 'shedding telemetry' /home/hari/collector.log"
   ```

   That counter is **cumulative since the collector started**, so a non-zero
   total is history, not an incident — this deployment reads 7,991 from
   outages that are long over. Read it twice a minute apart: what matters is
   whether it is still increasing.

### `ingest_worker_stale`
Two severities, and they mean different things:

* **WARNING** — no heartbeat, but telemetry is still arriving. Something is
  draining the stream, so this is a monitoring blind spot (usually an older
  worker build), not an outage.
* **CRITICAL** — no heartbeat *and* nothing arriving. Restart the worker (§2.2).

### `ingest_stalled`
No telemetry for three poll intervals, or none ever written. The absence of
device alarms right now means nothing is being measured — **not** that the
datacenter is well.

### `assignment_stale`
A collector is polling a plan more than 5 minutes old. It will pick up a new one
within 30 s; if it does not, the collector cannot reach the API.

---

## 5. Capacity and limits

Measured on the development host, which shares eight cores with Postgres,
Redis, the collector, the worker and a 664-device simulator. Treat them as
shape, not as product specifications.

### 5.1 API throughput
Held at **~50 requests/second** at both 25 and 50 concurrent clients while
latency doubled — flat throughput under rising concurrency is saturation. p95
was 1.2 s at 50 clients against a 500 ms target. The connection pool is
`db_pool_size` 10 + `db_max_overflow` 20 = **30**, below the 50-client target
and worth raising first, but it is not the whole story.

### 5.2 Stream caps
```
telemetry.v1     8,000 entries      ~49 KB per entry
events.v1      200,000 entries      25x the headroom, on purpose
```

Both are capped rings, so both sit **at** their cap in steady state. Depth is
not a health metric here; `dcim_collector_publish_dropped_total` is.
Events get far more room because a shed sample is a gap in a chart while a shed
event is a state change nobody ever hears about.

**Do not raise `telemetry` maxlen without raising Redis `maxmemory` first, in
that order.** At the previous 2,000,000 the stream reached tens of gigabytes on
a 3.7 GB host and **Redis was OOM-killed twice before the trim ever fired**.

### 5.3 The buffer window
The cap that prevents the OOM also bounds how long an outage can be absorbed
without loss. A **5-minute Postgres outage sheds telemetry** — measured. A
shorter one costs nothing. Events survive either way.

### 5.4 What survives what

| Outage | Telemetry | Events | Recovery |
|---|---|---|---|
| Redis, 2 min | shed | kept | automatic, then a backlog replay |
| Postgres, 5 min | **shed** | kept | automatic; 45,471 buffered samples drained |
| Collector | none collected | — | on restart; counter baselines reset |
| Worker | buffered to the cap | buffered | ~1 s after restart |

---

## 6. Routine operations

### 6.1 Upgrading
```bash
cd /home/hari/dcim-platform && git pull
./backend/.venv/bin/python -m alembic upgrade head     # with §2.1 env sourced
# restart worker, then API, then collector
```
Restart the **worker before the API**: the worker owns migrations-sensitive
write paths, and an API on new code against an old worker is the skew that
produces `ingest_worker_stale` warnings.

### 6.2 Adding a collector
1. Mint a scoped token — never reuse the fleet-wide one:
   ```python
   from app.core.security import mint_collector_token
   mint_collector_token("col-2")
   ```
2. Start it with that token and its own `collector_id`.
3. Confirm the split:
   ```bash
   curl -s -H "Authorization: Bearer $JWT" \
        http://127.0.0.1:8000/api/v1/collector/health | jq .shards
   ```
   `owned` must sum to the fleet, `unassigned` must be 0.

**The hazard.** Registering a collector that then does not run **strands its
shard**: those endpoints are owned by nobody and are "not mine" from every
other collector's point of view. `collector_stale` tells you a collector is
gone; `shards.owned_by_unhealthy` tells you how much of the fleet went with it.

### 6.3 Removing a collector
Delete its row, or its shard stays stranded:
```sql
DELETE FROM collector_instance WHERE id = 'col-2';
```
Its endpoints redistribute on the next assignment fetch. Roughly 1/N of the
fleet changes hands, and **each moved endpoint loses its counter baseline** —
expect a gap, not a spike, in interface rates for one poll cycle.

### 6.4 Backup
```bash
docker exec dcim-postgres-1 pg_dump -U dcim -Fc dcim > dcim-$(date +%F).dump
```
Telemetry hypertables dominate the size. Inventory alone (device, endpoint,
credential, rack, room) is small and is the part that cannot be re-derived —
telemetry can be re-collected, a credential cannot.

`DCIM_CREDENTIAL_KEY` is **not** in the database. A dump without that key
restores a system whose stored credentials cannot be decrypted. Back it up
separately, and never into the same store.

---

## 7. Diagnostic cookbook

```sql
-- Is anything arriving? Run twice, 30 s apart.
SELECT count(*) FROM telemetry_sample;

-- How stale, and has anything ever been written?
SELECT extract(epoch FROM (now() - max(ts))) AS age_s, count(*) > 0 AS ever
  FROM telemetry_sample;

-- Open platform alarms (device_id IS NULL means it is about the platform)
SELECT alarm_type, instance, severity, message, first_seen
  FROM alarm WHERE device_id IS NULL AND state <> 'CLEARED' ORDER BY last_seen DESC;

-- Endpoint status census
SELECT status, count(*) FROM endpoint_state GROUP BY 1;

-- Who was handed credentials, and when
SELECT ts, actor, action, target_id, outcome
  FROM audit_log WHERE action LIKE 'credential.%' ORDER BY id DESC LIMIT 20;
```

```bash
# Collector internals
curl -s http://127.0.0.1:9100/metrics | grep -E '^(go_goroutines|process_open_fds|dcim_collector_publish_dropped_total)'

# Backend internals (needs the API on 6.1 or newer)
curl -s http://127.0.0.1:8000/metrics | grep -E '^dcim_(ingest_lag|telemetry_age)'

# Readiness, including the ingest gate
curl -s http://127.0.0.1:8000/api/v1/ready | python3 -m json.tool
```

Logs: `/home/hari/{collector,worker,api}.log`. Poll outcomes are **not** in the
logs by design — a per-poll line at 1,386 endpoints is 1,300 lines a minute of
nothing. They go to metrics.

---

## 8. Known gaps, and what they mean for you

These are real and currently unfixed. An operator should know them before they
are discovered during an incident.

**Endpoints do not go UNKNOWN when the collector dies.** They go
ONLINE → DEGRADED. DEGRADED reads as "the device is having trouble", which is a
claim about the device made when the only thing that failed was the monitoring.
The platform correctly avoids marking them OFFLINE. Trust `collector_stale`
over the endpoint status during a collector outage.

**`poll_result` is empty.** The table, its retention policy and its writer all
exist; nothing publishes to it. There is no per-endpoint poll history — "which
endpoint failed, how often, with what error" cannot be answered. Use the
collector's Prometheus metrics, which are aggregate only.

**Forecasting needs 14 qualifying days**, where a day qualifies with data in 20
of 24 hours. A laptop that sleeps overnight never accumulates one, and
`/analytics/forecast` will return `insufficient_history` indefinitely. That is
the intended refusal, not a bug.

**A 5-minute database outage loses telemetry.** See §5.3.

**No user accounts.** Roles come from a single environment admin; there is no
user table, no refresh tokens, and no login lockout. RBAC is enforced on routes
but there is only one user to enforce it against.

---

## 9. Test and chaos tooling

```bash
python tools/chaos/chaos.py list                 # §7 fault scenarios
python tools/loadgen/loadgen.py query-load --clients 50 --seconds 60
```

The chaos scenarios **kill live services**. They restore in a `finally` block
and verify the restore, but nothing is supervised — check §2 afterwards. Read
`tools/chaos/README.md` before running one on anything that matters.
