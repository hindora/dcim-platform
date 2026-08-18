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

```bash
# 1. contracts → generated code (pure Python, no protoc needed)
make generate

# 2. infrastructure
cp deploy/.env.example deploy/.env      # then edit the secrets
docker compose -f deploy/docker-compose.yml up -d postgres redis

# 3. backend
cd backend
python -m venv .venv && . .venv/Scripts/activate    # Windows
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --port 8000

# 4. seed inventory from the simulator
python -m app.importer.cli --base-url http://127.0.0.1:8001 \
                           --username admin --password admin1234

# 5. ingest worker (separate shell)
python -m app.ingest.worker

# 6. collector (separate shell)
cd collector && go run ./cmd/collector --config configs/collector.yaml

# 7. frontend
cd frontend && npm install && npm run dev
```

API docs at <http://localhost:8000/docs>, UI at <http://localhost:5173>.

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
