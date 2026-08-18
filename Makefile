.PHONY: help setup generate check-generated mapcheck backend-test backend-lint \
        collector-build collector-test frontend-build lint test up down logs \
        dev dev-api dev-ingest dev-collector dev-ui seed

# Prefer the POSIX venv layout; fall back to the Windows one.
PY := $(shell if [ -x backend/.venv/bin/python ]; then echo backend/.venv/bin/python; \
              else echo backend/.venv/Scripts/python.exe; fi)
COMPOSE := docker compose -f deploy/docker-compose.yml --env-file deploy/.env

help:
	@echo "setup            one-time: venv, python deps, npm deps, collector build"
	@echo "up / down        postgres + redis in Docker"
	@echo "dev              api + ingest + collector + ui (run this inside WSL)"
	@echo "dev-api          just the API          dev-ingest    just the ingest worker"
	@echo "dev-collector    just the collector    dev-ui        just the frontend"
	@echo "seed             import inventory from the simulator"
	@echo "test             every gate: contracts, backend, collector, frontend"
	@echo "lint             ruff + go vet + gofmt + tsc"

# ----------------------------------------------------------------- setup

setup:
	./scripts/setup.sh

generate:
	$(PY) contracts/codegen.py

check-generated:
	$(PY) contracts/codegen.py --check

mapcheck:
	$(PY) contracts/mapcheck.py

# ------------------------------------------------------------- datastores

up:
	$(COMPOSE) up -d postgres redis

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

# --------------------------------------------------------------- running

dev:
	./scripts/dev.sh

dev-api:
	./scripts/dev.sh api

dev-ingest:
	./scripts/dev.sh ingest

dev-collector:
	./scripts/dev.sh collector

dev-ui:
	./scripts/dev.sh ui

seed:
	cd backend && ../$(PY) -m app.importer.cli

# ----------------------------------------------------------------- checks

backend-test:
	cd backend && ../$(PY) -m pytest -q

backend-lint:
	cd backend && ../$(PY) -m ruff check app tests

collector-build:
	cd collector && go mod tidy && go build ./...

collector-test:
	cd collector && gofmt -l . && go vet ./... && go test ./...

frontend-build:
	npm --prefix frontend run build

lint: backend-lint
	cd collector && gofmt -l . && go vet ./...
	npm --prefix frontend run typecheck

test: check-generated mapcheck backend-test collector-test frontend-build
