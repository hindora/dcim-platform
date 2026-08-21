# Load generation

Synthetic endpoints that answer **real SNMP**, seeded into inventory so the
collector picks them up through the ordinary assignment path. Nothing in the
collector or the backend knows it is being tested.

The pass criteria come from `docs/14-testing-strategy.md` §6.

## Prerequisites

* A running platform: Postgres, Redis, the backend, the ingest worker and the
  collector.
* A **second** snmpsim instance — never the one serving the real fleet. See
  *Known blocker* below before assuming this works.
* The backend virtualenv, for `sqlalchemy`, `httpx` and `websockets`.

Environment: `DCIM_DATABASE_URL`, `DCIM_CREDENTIAL_KEY`, `DCIM_COLLECTOR_TOKEN`,
`DCIM_JWT_SECRET`, `DCIM_ADMIN_PASSWORD`.

## Running

```bash
# 1. datasets, one .snmprec per synthetic agent
python loadgen.py generate --count 5000 --out ~/loadgen/data

# 2. the responder (community selects the agent; the cache dir must be writable
#    by this user - snmpsim defaults to /tmp/snmpsim, which is often root-owned)
snmpsim-command-responder \
    --data-dir=$HOME/loadgen/data \
    --cache-dir=$HOME/loadgen/cache \
    --agent-udpv4-endpoint=0.0.0.0:1161

# 3. inventory, in an isolated LOADTEST datacenter
python loadgen.py seed --count 5000 --interval 30

# 4. measure: two snapshots and a diff, because the counters are cumulative
python loadgen.py measure --save /tmp/before.json
sleep 3600
python loadgen.py measure --save /tmp/after.json
python loadgen.py compare /tmp/before.json /tmp/after.json

# 5. the tests that need no synthetic endpoints
python loadgen.py query-load --clients 50 --seconds 60
python loadgen.py ws-fanout --clients 200 --topics 20 --seconds 60

# 6. remove everything, including the telemetry it wrote
python loadgen.py teardown
```

`teardown` deletes telemetry by device id before dropping the devices. The
hypertables carry no foreign key to `device`, so a cascade alone leaves orphan
samples that keep answering analytics queries — a synthetic room contributing
load to a capacity report for a room that no longer exists.

## Design notes

**Communities select the agent, addresses stay distinct.** snmpsim routes a
request to `<community>.snmprec`, so one process serves thousands of agents.
Each endpoint still gets its own destination address (127.16.x.y — the whole of
127/8 is locally routable on Linux with no interface aliases), because pointing
5,000 endpoints at one address measures a scheduler and calls it a network.

**The datasets answer the OIDs the collector actually asks for**, taken from
`contracts/mappings/snmp/standard.yaml`. An agent that responds without carrying
the mapped OIDs still counts as a successful poll while returning nothing, which
inflates completion, deflates every latency, and produces a load test that
passes by measuring an empty conversation.

**snmprec files must be sorted numerically by OID arc.** Sorted as text, `.10`
precedes `.2`, GETNEXT walks out of order, and the agent looks broken.

## Known blocker: snmpsim in this environment

The responder could not be started on the development machine. The lightweight
entry point fails immediately:

```
AttributeError: 'AsyncioDispatcher' object has no attribute 'registerRecvCbFun'
```

The full responder does not crash: it binds the UDP socket — so `ss` shows a
listener and the log prints *"Listening at UDP/IPv4 endpoint 0.0.0.0:1161"* —
but never registers a receive callback, so every request times out with no entry
in its log. Bound and deaf is a worse failure than a crash, because everything
looks correct.

This is a version mismatch between the installed `snmpsim` and `pysnmp` in the
only virtualenv available here. The simulator's own responder still works
because it is a long-lived process started before the mismatch; restarting it to
confirm would take the live fleet down.

To run steady-state, install a matched `snmpsim`/`pysnmp` pair in a dedicated
virtualenv and point step 2 at it. Everything either side of the responder is
verified working: seeding, assignment pickup, polling, teardown.

## What has been measured, and on what

Measured on a shared development laptop — 8 GB total with roughly 0.5 GB free,
WSL2 limited to 3.7 GB with about 1 GB available, 8 cores, and Postgres, Redis,
the collector, the ingest worker and a 664-device simulator all running on the
same box. These are not clean numbers and they are not meant to be published as
the platform's capacity.

| Test | Criterion | Result |
|---|---|---|
| Steady state, 5,000 @ 30 s | completion > 99.5 %, p95 poll < 1 s | **not run** — responder blocker, and the RAM headroom is not there |
| Query load, 50 clients | API p95 < 500 ms | **fail**: p95 1.217 s, p50 0.450 s, p99 10.4 s, 0 errors |
| Query load, 25 clients | (below pool limit) | p95 0.950 s, p50 0.231 s, p99 3.6 s |
| WS fan-out, 200 × 20 | no slow-consumer disconnects | **pass**: 200/200 connected, 0 drops |
| Burst, 10,000 traps in 60 s | no drops, alarm p95 < 5 s | **not run** — needs the trap receiver harness |
| Soak, 72 h | flat fds, goroutines, memory | **not run** — wall-clock bound, belongs to 6.6 |

Throughput held at ~50 requests/second at both 25 and 50 clients while latency
doubled. Flat throughput under rising concurrency is saturation, not queueing
alone: the server is at its ceiling on this hardware. The connection pool
(`db_pool_size` 10 + `db_max_overflow` 20 = 30) is below the 50-client target
and is worth raising, but it is not the whole story.

Every endpoint degraded uniformly — `/collector/instances`, a trivial query,
reached p95 1.12 s alongside `/dashboard/summary` at 1.96 s. A single slow query
degrades one path; a shared resource degrades all of them.

The websocket run proves 200 concurrent connections with 20 topics each and no
slow-consumer disconnects. It does **not** prove fan-out throughput: the
subscribed topics were quiet during the window, so each client received one
frame. Driving telemetry through those exact topics is the missing half.

Collector runtime at the fleet's normal 1,386 endpoints, for a baseline to
compare a future 5,000-endpoint run against: 524 goroutines, 149 open fds,
52 MB RSS.

## A gap this phase found

`poll_result` is empty — 0 rows. The table exists, has a 14-day retention
policy, and `app/ingest/writer.py::record_poll_results` is written and never
called. No contract message carries a poll outcome and the collector never
publishes one. Per-poll forensics — which endpoint failed, how often, with what
error class — has nowhere to land, and §6's completion criterion has to be read
from collector metrics instead. Wiring it touches the contract, the collector
and the worker, which is why it is reported here rather than done inside a load
test.
