#!/usr/bin/env bash
#
# Run the DCIM stack for development.
#
#   ./scripts/dev.sh              API + ingest worker + collector + UI
#   ./scripts/dev.sh api ingest   only those
#   ./scripts/dev.sh collector    only the collector
#
# Datastores are NOT started here: Postgres and Redis run in Docker
# (deploy/docker-compose.yml) and this script waits for them.
#
# Where this must run
#   The collector has to see the device plane. In this setup the simulated
#   device IPs live on an interface inside WSL, so a collector started from
#   Windows cannot reach them however well it is configured. Run this script
#   inside WSL and everything lines up: WSL reaches the Docker-published
#   Postgres and Redis over 127.0.0.1 (mirrored networking), and reaches the
#   devices directly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="${ENV_FILE:-$ROOT/deploy/.env}"
VENV="${VENV:-$ROOT/backend/.venv}"
LOG_DIR="$ROOT/var/log"
mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------- helpers

c_reset=$'\033[0m'; c_dim=$'\033[2m'; c_red=$'\033[31m'; c_yellow=$'\033[33m'
say()  { printf '%s==>%s %s\n' "$c_dim" "$c_reset" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$c_yellow" "$c_reset" "$*" >&2; }
die()  { printf '%s[error]%s %s\n' "$c_red" "$c_reset" "$*" >&2; exit 1; }

wait_port() {
  local host=$1 port=$2 label=$3 tries=${4:-30}
  for _ in $(seq 1 "$tries"); do
    if timeout 2 bash -c "</dev/tcp/$host/$port" 2>/dev/null; then return 0; fi
    sleep 1
  done
  die "$label is not reachable at $host:$port.
     Start the datastores first:
       docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d postgres redis"
}

# ------------------------------------------------------------- environment

[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE - copy deploy/.env.example and fill it in.
     If this machine already has data, the file must carry the SAME
     DCIM_CREDENTIAL_KEY that encrypted the stored device credentials, or the
     collector assignment cannot be decrypted."

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

: "${POSTGRES_USER:=dcim}" "${POSTGRES_DB:=dcim}"
: "${DB_HOST:=127.0.0.1}" "${DB_PORT:=5432}"
: "${REDIS_HOST:=127.0.0.1}" "${REDIS_PORT:=6379}"

# 127.0.0.1 rather than localhost: on Windows and under WSL, localhost can
# resolve to ::1 first and a Docker-published port answers there unreliably.
export DCIM_DATABASE_URL="${DCIM_DATABASE_URL:-postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${DB_HOST}:${DB_PORT}/${POSTGRES_DB}}"
export DCIM_REDIS_URL="${DCIM_REDIS_URL:-redis://:${REDIS_PASSWORD}@${REDIS_HOST}:${REDIS_PORT}/0}"

PY="$VENV/bin/python"
[[ -x "$PY" ]] || PY="$VENV/Scripts/python.exe"          # Windows layout
[[ -x "$PY" ]] || die "no virtualenv at $VENV - run: make setup"

# ------------------------------------------------------------- what to run

components=("$@")
if [[ ${#components[@]} -eq 0 ]]; then
  components=(api ingest collector ui)
fi

want() { [[ " ${components[*]} " == *" $1 "* ]]; }

# ------------------------------------------------------------ preflight

wait_port "$DB_HOST" "$DB_PORT" "PostgreSQL"
wait_port "$REDIS_HOST" "$REDIS_PORT" "Redis"
say "datastores reachable"

port_free() {
  ! timeout 1 bash -c "</dev/tcp/127.0.0.1/$1" 2>/dev/null
}

# A component that cannot bind its port dies quietly while something else keeps
# answering there, which is far more confusing than refusing to start.
if want api && ! port_free "${API_PORT:-8000}"; then
  die "port ${API_PORT:-8000} is already in use - stop the other API first"
fi
if want ui && ! port_free 5173; then
  die "port 5173 is already in use - stop the other dev server first"
fi

if want collector; then
  # A collector that cannot see the device plane will start happily and time
  # out on every poll, which reads as "all devices down" rather than as a
  # misplaced process. Say so up front.
  # `ip` lives in /usr/sbin, which is absent from PATH in a non-login shell -
  # so calling it bare made this check silently fail and warn on a host where
  # the devices were in fact reachable.
  IP_BIN="$(command -v ip || true)"
  [[ -n "$IP_BIN" ]] || IP_BIN=/usr/sbin/ip
  if [[ ! -x "$IP_BIN" ]] || ! "$IP_BIN" -4 -o addr show 2>/dev/null        | grep -qE '10\.(50|51|52)\.'; then
    warn "no 10.50/10.51/10.52 addresses on this host: the collector will not
       reach the simulated devices. Run this inside WSL, where they are bound."
  fi
fi

if want api || want ingest; then
  say "applying migrations"
  (cd backend && "$PY" -m alembic upgrade head >"$LOG_DIR/alembic.log" 2>&1) \
    || { cat "$LOG_DIR/alembic.log"; die "alembic upgrade failed"; }
fi

# --------------------------------------------------------------- launching

pids=()
labels=()

start() {
  local label=$1; shift
  say "starting $label"
  ( "$@" 2>&1 | sed -u "s/^/[$label] /" ) &
  pids+=("$!")
  labels+=("$label")
}

cleanup() {
  local status=$?
  say "stopping"
  for pid in "${pids[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  exit "$status"
}
trap cleanup INT TERM EXIT

if want api; then
  start api "$PY" -m uvicorn app.main:app \
    --host 0.0.0.0 --port "${API_PORT:-8000}" --app-dir backend --log-level warning
fi

if want ingest; then
  # More than one is now safe. It was not: the workers split the batches
  # between them, which is what a consumer group is for and loses nothing - but
  # the counter baselines were a read-modify-write on shared Redis keys with a
  # database transaction inside the window, so two workers computed their
  # deltas from the same previous reading and an interface's throughput came
  # out silently wrong. The baseline exchange is now a single atomic step, so
  # each worker gets a different previous value and the deltas chain.
  #
  # Still one by default. Measured on this fleet, a single worker sits 0.3 s
  # behind the newest entry and advances 66.7 s per 66 s of wall clock; there
  # is no throughput deficit to spend a process on. Raise it when the
  # measurement says to, not in advance.
  for i in $(seq 1 "${INGEST_WORKERS:-1}"); do
    start "ingest${i}" env PYTHONPATH="$ROOT/backend" "$PY" -m app.ingest.worker
  done
fi

if want collector; then
  BIN="$ROOT/collector/bin/collector"
  if [[ ! -x "$BIN" ]]; then
    if command -v go >/dev/null 2>&1; then
      say "building collector"
      (cd collector && go build -o bin/collector ./cmd/collector)
    else
      die "no collector binary at $BIN and no Go toolchain.
     Install Go, or cross-compile elsewhere with:
       GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -o collector/bin/collector ./cmd/collector"
    fi
  fi
  start collector env -C "$ROOT/collector" "$BIN" --config configs/collector.yaml
fi

if want ui; then
  [[ -d frontend/node_modules ]] || die "frontend dependencies missing - run: make setup"
  start ui npm --prefix frontend run dev -- --host 0.0.0.0
fi

say "running: ${labels[*]}"
say "API  http://127.0.0.1:${API_PORT:-8000}/docs"
# `want ui && say ...` would abort the script under `set -e` whenever the UI
# was not selected, because the compound statement returns non-zero.
if want ui; then say "UI   http://127.0.0.1:5173"; fi
say "Ctrl-C to stop"

wait
