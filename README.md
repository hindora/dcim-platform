# DCIM Platform

A DCIM / infrastructure monitoring platform. Collects telemetry from datacenter
infrastructure over SNMP, gNMI, BACnet, Redfish and Modbus, normalises it into a
single canonical model, and serves inventory, topology, alarms and analytics.

Its device plane today is the [Datacenter Network Simulator](../DCIM/Datacenter_Network_Simulator);
the architecture is deliberately built so the same platform can monitor real
hardware without a rewrite.

## Architecture

```
Devices ──SNMP/gNMI/BACnet/Redfish/Modbus──▶ Go Collector
                                                  │ publish (msgpack)
                                                  ▼
                                          Redis Streams  telemetry.v1 / events.v1
                                                  │
                                                  ▼
                                          Python Ingest Worker  ── sole DB writer
                                                  │
                          PostgreSQL + TimescaleDB │ Redis pub/sub
                                                  ▼
                                          FastAPI  ── REST + WebSocket
                                                  ▼
                                          React + TypeScript UI
```

Hard boundaries:

| Plane | Owns | Never does |
|---|---|---|
| Go collector | wire protocols, normalisation, endpoint health | touch the database, know what a rack is |
| Ingest worker | the only writes to Postgres/Timescale, rules, alarms | serve HTTP |
| FastAPI | REST, WebSocket, business logic | poll devices |
| React | presentation | compute KPIs |

Full design: [`docs/`](docs/) — start with `docs/README.md`.

## Repository layout

| Path | Contents |
|---|---|
| `contracts/` | the versioned contract: message schema, metric registry, protocol mappings, codegen |
| `collector/` | Go collector |
| `backend/` | FastAPI app, ingest worker, migrations |
| `frontend/` | React + TypeScript + Vite UI |
| `deploy/` | docker compose and environment templates |
| `docs/` | design documentation |

## Prerequisites

| Tool | Version | Needed for |
|---|---|---|
| Python | 3.11+ | backend |
| Go | 1.22+ | collector |
| Node | 20+ | frontend |
| Docker + Compose | recent | Postgres/TimescaleDB, Redis, full stack |

## Quick start

The collector must run where it can see the devices. In this deployment the
simulated device IPs are bound inside WSL, so a collector started from Windows
cannot reach them no matter how it is configured. Run the whole stack inside
WSL and point it at Docker on Windows - WSL reaches published ports over
127.0.0.1 under mirrored networking.

```bash
# in WSL, on the WSL filesystem (NOT /mnt/c - DrvFs makes pip and npm crawl)
git clone https://github.com/hindora/dcim-platform.git ~/dcim-platform
cd ~/dcim-platform

cp deploy/.env.example deploy/.env    # then fill in the secrets
make setup                            # venv, python deps, npm deps, collector
make up                               # postgres + redis in Docker
make dev                              # api + ingest + collector + ui
```

If the machine already holds data, `deploy/.env` must carry the **same**
`DCIM_CREDENTIAL_KEY` that encrypted the stored device credentials - a new key
cannot decrypt them.

Seed the inventory once the API is up:

```bash
make seed     # or: python -m app.importer.cli --file <topology-export.json>
```

API docs at <http://localhost:8000/docs>, UI at <http://localhost:5173>.

Individual components: `make dev-api`, `make dev-ingest`, `make dev-collector`,
`make dev-ui`. Run exactly one ingest worker in development: two in the same
consumer group split batches between them, which looks like data going missing.

## Make targets

| Target | Does |
|---|---|
| `make generate` | regenerate Go/Python/TS code from `contracts/` |
| `make check-generated` | fail if generated code is stale (CI gate) |
| `make backend-test` | pytest |
| `make collector-test` | go test |
| `make frontend-build` | vite build |
| `make lint` | ruff + go vet + tsc |

## Status

Phases 0 and 1 of `docs/15-implementation-roadmap.md` are implemented:
foundations, the contract, the SNMP vertical slice from device to browser.
Phases 2+ (traps, alarms, WebSocket, remaining protocols) are not yet built.
