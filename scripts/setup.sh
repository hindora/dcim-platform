#!/usr/bin/env bash
#
# One-time developer setup: Python venv, frontend dependencies, collector build.
#
#   ./scripts/setup.sh
#
# Datastores are not touched here - they live in Docker and are started with
# `make up`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

c_reset=$'\033[0m'; c_dim=$'\033[2m'; c_yellow=$'\033[33m'
say()  { printf '%s==>%s %s\n' "$c_dim" "$c_reset" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$c_yellow" "$c_reset" "$*" >&2; }

# A checkout on /mnt/c under WSL works but is slow enough to be miserable:
# DrvFs makes pip and npm crawl and breaks file watching. Say so once.
case "$ROOT" in
  /mnt/*) warn "this checkout is on a Windows drive ($ROOT).
       Under WSL that is slow for pip/npm and unreliable for file watching.
       Prefer a clone inside the WSL filesystem, e.g. ~/dcim-platform." ;;
esac

# --------------------------------------------------------------- python

PYBIN="${PYTHON:-python3}"
command -v "$PYBIN" >/dev/null 2>&1 || PYBIN=python
command -v "$PYBIN" >/dev/null 2>&1 || { echo "no python found" >&2; exit 1; }

say "python: $("$PYBIN" --version 2>&1)"
if [[ ! -d backend/.venv ]]; then
  say "creating backend/.venv"
  "$PYBIN" -m venv backend/.venv
fi

VPY="backend/.venv/bin/python"
[[ -x "$VPY" ]] || VPY="backend/.venv/Scripts/python.exe"

say "installing backend dependencies"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -e "backend[dev]"

say "regenerating contract code"
"$VPY" contracts/codegen.py
"$VPY" contracts/mapcheck.py

# -------------------------------------------------------------- frontend

if command -v npm >/dev/null 2>&1; then
  say "installing frontend dependencies"
  npm --prefix frontend install --no-audit --no-fund --silent
else
  warn "npm not found - skipping the frontend"
fi

# ------------------------------------------------------------- collector

if command -v go >/dev/null 2>&1; then
  say "building collector ($(go version | awk '{print $3}'))"
  (cd collector && go mod tidy >/dev/null && go build -o bin/collector ./cmd/collector)
else
  warn "go not found - skipping the collector build.
       Install Go, or cross-compile elsewhere with:
         GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -o collector/bin/collector ./cmd/collector"
fi

# ------------------------------------------------------------------ env

if [[ ! -f deploy/.env ]]; then
  warn "deploy/.env does not exist yet.
       Copy deploy/.env.example and fill it in. If this machine already holds
       data, reuse the SAME DCIM_CREDENTIAL_KEY that encrypted the stored
       device credentials - a new key cannot decrypt them."
fi

say "done. Next:"
echo "    make up      # postgres + redis in Docker"
echo "    make dev     # api + ingest + collector + ui"
