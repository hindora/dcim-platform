.PHONY: help generate check-generated mapcheck backend-install backend-test \
        backend-lint collector-build collector-test frontend-install \
        frontend-build lint test up down seed

PY ?= backend/.venv/Scripts/python.exe   # on Linux/macOS: backend/.venv/bin/python

help:
	@echo "generate         regenerate Go/Python/TS code from contracts/"
	@echo "check-generated  fail if generated code is stale (CI gate)"
	@echo "mapcheck         validate protocol mappings against the metric registry"
	@echo "backend-test     pytest"
	@echo "collector-test   go vet + go test"
	@echo "frontend-build   typecheck + vite build"
	@echo "test             everything"
	@echo "up / down        docker compose dev stack"
	@echo "seed             import inventory from the simulator"

generate:
	$(PY) contracts/codegen.py

check-generated:
	$(PY) contracts/codegen.py --check

mapcheck:
	$(PY) contracts/mapcheck.py

backend-install:
	cd backend && python -m venv .venv && $(PY) -m pip install -e ".[dev]"

backend-test:
	cd backend && ../$(PY) -m pytest -q

backend-lint:
	cd backend && ../$(PY) -m ruff check app tests

collector-build:
	cd collector && go mod tidy && go build ./...

collector-test:
	cd collector && go vet ./... && go test ./...

frontend-install:
	cd frontend && npm install

frontend-build:
	cd frontend && npm run build

lint: backend-lint
	cd collector && go vet ./...
	cd frontend && npm run typecheck

test: check-generated mapcheck backend-test collector-test frontend-build

up:
	docker compose -f deploy/docker-compose.yml up -d

down:
	docker compose -f deploy/docker-compose.yml down

seed:
	cd backend && ../$(PY) -m app.importer.cli
