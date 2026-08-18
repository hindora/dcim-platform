# DCIM Collector

Go service that polls datacenter infrastructure, normalises everything into the
canonical telemetry model, and publishes it onto Redis Streams.

It **never** writes to the database. The ingest worker is the only writer; the
stream between them is both the versioned contract and the buffer that keeps
collection running when the database is slow.

## Build and run

```bash
go mod tidy        # resolves go.sum - required on a fresh clone
go build ./...
go vet ./...
go test ./...

export DCIM_COLLECTOR_TOKEN=...      # must match the backend's DCIM_COLLECTOR_TOKEN
export DCIM_REDIS_URL=redis://localhost:6379/0
go run ./cmd/collector --config configs/collector.yaml
```

`go.sum` is intentionally not committed pre-resolved; run `go mod tidy` once.

## What it does on startup

1. Loads `contracts/mappings/snmp/*.yaml` and validates every metric key against
   the generated registry. A typo fails at boot, not silently at runtime.
2. Fetches its assignment from the DCIM API (synchronously, so the startup log
   is honest about how many endpoints it owns).
3. Schedules each endpoint at a deterministic phase within its interval, so 664
   endpoints on a 30 s cycle fire ~22 per second rather than all at once.
4. Publishes telemetry in batches, endpoint state on change only, and a
   heartbeat every 10 s.

## Endpoints

| Address | Purpose |
|---|---|
| `:9100/metrics` | Prometheus |
| `:9101/health` | liveness |
| `:9101/ready` | Redis + assignment + adapters |

`/ready` returning 503 does **not** stop collection: it only removes the
instance from a load balancer. The collector keeps polling its last known
assignment, because falling back to "no endpoints" would silently stop
everything.

## SNMP specifics for this device plane

- **The community string is the agent's IP address**, never `public`. A wrong
  community produces no response at all, which looks exactly like a dead device.
- A server has **two** SNMP endpoints: the OS agent on the production NIC and
  the BMC agent on the management IP. They are different agents with different
  MIBs.
- Five device types (RPP, chiller, pump, cooling tower, valve) carry no SNMP
  agent at all, and inventory does not create endpoints for them.
- `sysUpTime` is read in the same cycle as the counters. A decrease sets
  `CounterReset` on that cycle's samples so the ingest worker discards the delta
  instead of publishing a spike.

## Layout

```
cmd/collector        entrypoint
internal/app         wiring and lifecycle
internal/assign      assignment client (ETag polling, diffing)
internal/sched       time wheel, worker pool, per-protocol and per-host limits
internal/health      per-endpoint communication state machine
internal/publish     batching, backpressure, shedding
internal/mapping     loads contracts/mappings, validates against the registry
internal/adapters    protocol implementations (snmp today)
internal/obs         logging, metrics, health endpoints
pkg/models           generated contract types + shared domain types
```

## Adding a protocol

1. Implement `models.Adapter` under `internal/adapters/<name>/`.
2. Register it in `internal/app/app.go` and add a `protocols.<name>` config
   block with its concurrency and per-host limits.
3. Add `contracts/mappings/<name>/*.yaml`.
4. Widen the importer's `--protocols` flag so endpoints get created.

Nothing in the backend changes.
