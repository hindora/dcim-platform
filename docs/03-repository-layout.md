# 03 — Repository Layout

The DCIM is a **separate repository** from the simulator. The simulator is the
device plane; the DCIM must be able to point at real hardware without carrying
simulator code. A single monorepo with three workspaces is the right shape,
because the contract must be versioned once and consumed by all three.

```
dcim/
├── contracts/                       # THE versioned contract. Language-neutral. Changes here are releases.
│   ├── proto/
│   │   └── dcim/telemetry/v1/
│   │       ├── telemetry.proto      # Telemetry, Event, EndpointState, CollectorHeartbeat
│   │       └── common.proto         # Quality, ValueType, Severity, Protocol enums
│   ├── metrics/
│   │   └── registry.yaml            # canonical metric dictionary (see 06-metric-registry.md)
│   ├── mappings/                    # protocol → canonical, data not code
│   │   ├── snmp/
│   │   │   ├── standard.yaml        # RFC1213, IF-MIB, ENTITY-SENSOR-MIB, UPS-MIB
│   │   │   ├── vendor_apc.yaml
│   │   │   ├── vendor_raritan.yaml
│   │   │   ├── vendor_vertiv.yaml
│   │   │   └── traps.yaml           # vendor trap OID → canonical event_type + severity + clears
│   │   ├── bacnet/
│   │   │   └── plant.yaml           # object name → metric, per plant device type
│   │   ├── redfish/
│   │   │   └── resources.yaml       # JSON pointer → metric; MessageId → event_type
│   │   ├── gnmi/
│   │   │   └── openconfig.yaml      # path → metric
│   │   └── modbus/
│   │       └── registers.yaml       # register map per device model
│   ├── openapi/
│   │   └── dcim-v1.yaml             # generated from FastAPI, committed for the UI codegen
│   └── Makefile                     # protoc → Go + Python; registry → Go consts + Python enums + TS types
│
├── collector/                       # Go
│   ├── cmd/
│   │   ├── collector/main.go
│   │   └── mapcheck/main.go         # CLI: validate mapping YAML against the registry
│   ├── internal/
│   │   ├── app/                     # wiring, lifecycle, graceful shutdown
│   │   ├── assign/                  # assignment client (ETag polling + pubsub nudge), diffing
│   │   ├── sched/                   # time wheel, jitter, per-endpoint job state
│   │   ├── pool/                    # worker pool, per-protocol semaphores, rate limiting
│   │   ├── adapters/
│   │   │   ├── snmp/                # poller + trap receiver
│   │   │   ├── gnmi/                # subscribe manager, stream lifecycle
│   │   │   ├── bacnet/              # BACnet/IP client, MS/TP routing, COV
│   │   │   ├── redfish/             # poller + event receiver + subscription reconciler
│   │   │   └── modbus/              # TCP client, gateway unit-id fan-out
│   │   ├── mapping/                 # loads contracts/mappings, compiles to lookup tables
│   │   ├── normalize/               # protocol result → canonical Telemetry/Event
│   │   ├── health/                  # per-endpoint state machine, consecutive failures
│   │   ├── publish/                 # Redis Streams publisher, batching, backpressure, shedding
│   │   ├── config/                  # process config only — NO device list
│   │   ├── obs/                     # slog setup, Prometheus metrics, /health /ready
│   │   └── version/
│   ├── pkg/
│   │   └── models/                  # generated protobuf Go types + hand-written helpers
│   ├── configs/collector.yaml
│   ├── tests/                       # integration tests against the running simulator
│   ├── Dockerfile
│   └── go.mod
│
├── backend/                         # Python
│   ├── app/
│   │   ├── main.py                  # FastAPI app factory
│   │   ├── api/v1/
│   │   │   ├── devices.py  racks.py  rooms.py  rows.py  datacenters.py
│   │   │   ├── telemetry.py  alarms.py  events.py  topology.py
│   │   │   ├── power.py  cooling.py  environment.py  capacity.py  analytics.py
│   │   │   ├── dashboard.py  collector.py  discovery.py  admin.py
│   │   │   └── ws.py
│   │   ├── schemas/                 # Pydantic v2 request/response models
│   │   ├── models/                  # SQLAlchemy ORM
│   │   ├── repositories/            # all SQL lives here
│   │   ├── services/                # business logic; no SQL, no FastAPI imports
│   │   ├── ingest/
│   │   │   ├── worker.py            # consumer group loop, XAUTOCLAIM reclaim
│   │   │   ├── enrich.py            # inventory cache lookup
│   │   │   ├── rates.py             # counter → rate, wrap/discontinuity handling
│   │   │   ├── writer.py            # batched COPY + state upsert
│   │   │   └── fanout.py            # Redis pub/sub emit
│   │   ├── alarms/
│   │   │   ├── engine.py            # dwell, hysteresis, raise/update/clear
│   │   │   ├── rules.py             # rule model + loader
│   │   │   ├── correlation.py       # dependency suppression, root cause
│   │   │   └── lifecycle.py         # ack, clear, escalation
│   │   ├── topology/                # graph build + traversal (network/power/cooling/physical)
│   │   ├── analytics/               # pue.py capacity.py thermal.py forecast.py
│   │   ├── importer/                # simulator topology-export seed importer
│   │   ├── tasks/                   # arq task definitions
│   │   ├── websocket/               # connection manager, subscriptions, coalescer
│   │   ├── core/                    # config, security, logging, deps, errors
│   │   └── db/                      # session, base, migrations entrypoint
│   ├── alembic/versions/
│   ├── tests/{unit,integration,e2e}/
│   ├── pyproject.toml
│   └── Dockerfile
│
├── frontend/                        # React + TS + Vite
│   ├── src/
│   │   ├── api/                     # generated client from contracts/openapi + hand wrappers
│   │   ├── ws/                      # single WS client, reconnect, subscription manager
│   │   ├── store/                   # zustand slices: live state, alarms, selection
│   │   ├── components/              # primitives: StatusChip, MetricTile, TimeChart, RackFrame
│   │   ├── features/
│   │   │   ├── dashboard/  infrastructure/  it/  power/  cooling/
│   │   │   ├── environment/  topology/  monitoring/  analytics/  simulator/
│   │   ├── routes/                  # react-router route tree
│   │   ├── theme/                   # design tokens; no raw hex at call sites
│   │   └── lib/                     # units, formatting, colour scales, time helpers
│   ├── index.html  vite.config.ts  package.json
│   └── Dockerfile
│
├── deploy/
│   ├── docker-compose.yml           # dev: postgres+timescale, redis, api, ingest, worker, collector, ui
│   ├── docker-compose.sim.yml       # overlay that points the collector at a local simulator
│   ├── k8s/                         # later
│   └── grafana/                     # dashboards for the platform's own metrics (not the DCIM UI)
│
├── docs/                            # this document set, kept with the code
├── tools/
│   ├── seed_from_simulator.py       # one-shot inventory seed
│   ├── loadgen/                     # synthetic endpoint generator for scale tests
│   └── contract_diff.py             # fails CI if a proto/registry change is breaking
├── Makefile
└── README.md
```

## Rules that keep this structure honest

1. **`contracts/` is the only thing both planes import.** If the Go collector
   needs to know a Python type, or vice versa, the answer is a contract change,
   not an import.
2. **No SQL outside `backend/app/repositories/`.** Services take repositories as
   dependencies. This is what makes the service layer unit-testable without a
   database.
3. **No FastAPI imports in `services/`.** A service that imports `HTTPException`
   has business logic welded to a transport.
4. **No device list in `collector/configs/`.** Finding A7. The only device-shaped
   thing in collector config is the assignment endpoint URL.
5. **Mapping tables are data, not code.** `contracts/mappings/*.yaml` are loaded
   at boot. Adding an APC PDU OID must not require a Go release — and the
   `mapcheck` CLI validates every mapping references a metric that exists in the
   registry, so a typo fails CI rather than silently dropping a metric.
6. **Generated code is committed**, with a CI check that regeneration produces no
   diff. Contributors should not need `protoc` to build.
