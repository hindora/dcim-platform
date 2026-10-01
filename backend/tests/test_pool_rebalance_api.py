"""POST /pools/{id}/rebalance and its preview over HTTP, stubbed services."""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import pools as api
from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.main import create_app
from app.services import assigner

PREVIEW = {"pool_id": "p1", "endpoints": 8, "moving": 4, "moves": [],
           "before": {"a1": 8}, "after": {"a1": 4, "a2": 4}, "automatic": 0,
           "frozen": False, "balanced": False}


class _Session:
    async def commit(self):
        return None


@pytest.fixture
def client(monkeypatch):
    calls: list = []
    preview = dict(PREVIEW)

    async def get_pool(session, pid):
        return {"id": pid} if pid == "p1" else None

    async def rebalance_preview(session, pid):
        return dict(preview)

    async def run(session, force_pool=None):
        calls.append(force_pool)
        return types.SimpleNamespace(ran=True, moved=4)

    audits: list = []

    async def record(session, **kw):
        audits.append(kw)

    monkeypatch.setattr(api.repo, "get_pool", get_pool)
    monkeypatch.setattr(api.shard_map, "rebalance_preview", rebalance_preview)
    monkeypatch.setattr(assigner, "run", run)
    monkeypatch.setattr(api, "audit", types.SimpleNamespace(
        record=record, client_of=lambda r: ("127.0.0.1", "t"), actor_of=lambda p: p.username))
    app = create_app()

    async def session_override():
        yield _Session()
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[current_principal] = lambda: Principal("tester", "admin")
    tc = TestClient(app, raise_server_exceptions=False)
    tc.calls, tc.audits, tc.preview, tc.app_ = calls, audits, preview, app
    return tc


def test_preview_is_open_and_unknown_pools_are_404(client):
    client.app_.dependency_overrides[current_principal] = lambda: Principal("v", "viewer")
    assert client.get("/api/v1/pools/p1/rebalance-preview").json()["moving"] == 4
    assert client.get("/api/v1/pools/nope/rebalance-preview").status_code == 404


def test_rebalance_needs_admin(client):
    client.app_.dependency_overrides[current_principal] = lambda: Principal("o", "operator")
    assert client.post("/api/v1/pools/p1/rebalance").status_code == 403
    assert client.calls == []


def test_rebalance_forces_only_this_pool_and_is_audited(client):
    r = client.post("/api/v1/pools/p1/rebalance")
    assert r.status_code == 200 and r.json()["recorded_moves"] == 4
    assert client.calls == ["p1"]
    assert client.audits[0]["action"] == "pool.rebalance"


def test_a_change_freeze_refuses_and_moves_nothing(client):
    client.preview["frozen"] = True
    assert client.post("/api/v1/pools/p1/rebalance").status_code == 409
    assert client.calls == []
