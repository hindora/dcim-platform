"""GET /api/v1/anomalies/health (docs/30 section 11), over HTTP.

Viewer-readable (docs/31 D6). POST /login only ever issues an admin token
(api/v1/misc.py), so the viewer token here is minted by hand with
issue_token(..., "viewer"), exactly as docs/30's testing note says.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.analytics import sweep as anomaly_sweep
from app.api.v1 import anomalies as api
from app.core.config import get_settings
from app.core.security import issue_token
from app.db.session import get_session
from app.main import create_app

TOP_KEYS = {"enabled", "explainer", "sweep_s", "last_sweep_at", "last_sweep_ms",
            "findings_per_hour", "budget_per_hour", "lock_holder", "baseline_last_fit_at",
            "severity_cap", "l5_major_enabled", "detail"}


class _Redis:
    def __init__(self, store=None, log=None):
        self.store = store or {}
        self.log = log or []

    async def get(self, key):
        return self.store.get(key)

    async def lrange(self, key, a, b):
        return self.log if key == anomaly_sweep.SWEEP_LOG_KEY else []


def _client(monkeypatch, *, enabled: bool, redis=None, counts=None):
    calls = {"counts": 0}

    async def health_counts(session):
        calls["counts"] += 1
        return counts or {
            "findings": {"open": 3, "opened_24h": 5,
                         "by_lane": {"advisory": 2, "alert": 1},
                         "by_kind": {"sensor": 1, "process": 2, "undetermined": 0}},
            "findings_per_hour": 1, "incidents": {"open": 0},
            "baseline_last_fit_at": None, "l5_major_enabled": False}

    monkeypatch.setattr(api.repo, "health_counts", health_counts)
    app = create_app()
    settings = get_settings().model_copy(update={"anomaly_enabled": enabled})

    async def session_override():
        yield object()

    async def redis_override():
        yield redis or _Redis()

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[api.get_redis] = redis_override
    app.dependency_overrides[get_settings] = lambda: settings
    tc = TestClient(app, raise_server_exceptions=False)
    tc.calls = calls
    return tc


def _viewer() -> dict:
    return {"Authorization": f"Bearer {issue_token('v', 'viewer')}"}


def test_a_viewer_can_read_it_and_off_is_not_an_error(monkeypatch):
    tc = _client(monkeypatch, enabled=False)
    r = tc.get("/api/v1/anomalies/health", headers=_viewer())
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == TOP_KEYS
    assert body["enabled"] is False
    assert body["explainer"] == "none"
    assert body["sweep_s"] == 60
    assert body["severity_cap"] == "MINOR"
    assert body["last_sweep_at"] is None and body["lock_holder"] is None
    assert body["detail"]["sweep"]["runs_1h"] == 0
    # Feature off reads nothing.
    assert tc.calls["counts"] == 0


def test_no_token_is_401(monkeypatch):
    tc = _client(monkeypatch, enabled=False)
    assert tc.get("/api/v1/anomalies/health").status_code == 401


def test_enabled_reports_the_heartbeat_and_the_hour(monkeypatch):
    now = datetime.now(UTC)
    hb = {"consumer": "ingest-1:31", "started_at": (now - timedelta(seconds=5)).isoformat(),
          "finished_at": now.isoformat(), "duration_ms": 842, "series_scored": 0,
          "error": None}
    log = [json.dumps(e) for e in [
        {"at": now.timestamp(), "consumer": "ingest-1:31", "outcome": "ran", "ms": 842},
        {"at": now.timestamp() - 30, "consumer": "ingest-2:44", "outcome": "cadence"},
        {"at": now.timestamp() - 60, "consumer": "ingest-1:31", "outcome": "ran", "ms": 100},
        {"at": now.timestamp() - 90, "consumer": "ingest-2:44", "outcome": "cadence"},
        {"at": now.timestamp() - 7200, "consumer": "ingest-1:31", "outcome": "ran", "ms": 9},
    ]]
    fit = {"consumer": "ingest-1:31", "fit_date": now.date().isoformat(),
           "started_at": (now - timedelta(seconds=214)).isoformat(),
           "finished_at": now.isoformat(), "series_fitted": 0, "rejected": 0}
    redis = _Redis({anomaly_sweep.SWEEP_HEARTBEAT_KEY: json.dumps(hb),
                    anomaly_sweep.BASELINE_HEARTBEAT_KEY: json.dumps(fit)}, log)
    tc = _client(monkeypatch, enabled=True, redis=redis)
    r = tc.get("/api/v1/anomalies/health", headers=_viewer())
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == TOP_KEYS
    assert body["enabled"] is True
    assert body["lock_holder"] == "ingest-1:31"
    assert body["last_sweep_ms"] == 842
    assert body["findings_per_hour"] == 1
    assert body["budget_per_hour"] is None
    sweep = body["detail"]["sweep"]
    assert sweep == {"p95_ms_1h": 842, "runs_1h": 2, "gate_skips_1h": 2, "errors_1h": 0,
                     "last_error": None, "series_scored": 0}
    assert body["detail"]["baselines"]["last_fit_duration_s"] == pytest.approx(214, abs=1)
    assert body["detail"]["findings"]["by_lane"] == {"advisory": 2, "alert": 1}
    assert set(body["detail"]) == {"sweep", "baselines", "findings", "incidents", "explain"}


def test_a_redis_outage_degrades_to_nulls(monkeypatch):
    class _Down:
        async def get(self, key):
            raise ConnectionError("redis down")

        async def lrange(self, *a):
            raise ConnectionError("redis down")

    tc = _client(monkeypatch, enabled=True, redis=_Down())
    r = tc.get("/api/v1/anomalies/health", headers=_viewer())
    assert r.status_code == 200
    assert r.json()["last_sweep_at"] is None
