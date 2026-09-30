"""The /pools router over real HTTP via TestClient, against a stubbed
service layer - what this pins is the HTTP contract: status codes, auth
gating, the shapes a UI will bind to. The service's own logic is covered
by test_pools.py (pure) and test_pools_live.py (real database)."""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import pools as api
from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.main import create_app
from app.services import pools as svc

POOL = {"id": "p1", "name": "DC1/BMS", "site": "DC1", "plane": "bms",
        "datacenter_id": "dc-1", "cidrs": [], "trap_vip": None, "bbmd_settings": {},
        "rate_budget_points_per_s": None, "min_members": 1}


class _Session:
    async def commit(self):
        return None


class _Stub:
    """A recording stand-in for the service module's async functions."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.pools = {"p1": {**POOL, "members": [], "endpoints": 0, "protocols": {},
                             "unassigned": 0, "owned": 0, "healthy_members": 0,
                             "accepting_members": 0, "below_min_members": False,
                             "ranges": [{"cidr": "10.52.1.0/24", "enabled": True}]}}
        self.placed = 0

    async def overview(self, session):
        return {"pools": list(self.pools.values()), "unpooled": {}, "planes": ["bms"]}

    async def detail(self, session, pool_id):
        if pool_id not in self.pools:
            raise svc.PoolNotFoundError(pool_id)
        return self.pools[pool_id]

    async def create(self, session, payload):
        self.calls.append(("create", payload))
        if payload["name"] == "dup":
            raise svc.PoolConflictError("a pool already exists for that site and plane")
        if payload["name"] == "bad":
            raise svc.PoolError("trap_vip: 'x' is not an IP address")
        return {**POOL, **payload, "id": "p-new", "site": "DC1"}

    async def update(self, session, pool_id, changes):
        self.calls.append(("update", pool_id, changes))
        if pool_id not in self.pools:
            raise svc.PoolNotFoundError(pool_id)
        return ({"trap_vip": None}, changes) if changes else ({}, {})

    async def delete(self, session, pool_id):
        self.calls.append(("delete", pool_id))
        if pool_id not in self.pools:
            raise svc.PoolNotFoundError(pool_id)
        if self.placed:
            raise svc.PoolError(
                f"{self.placed} collector(s) are placed in this pool; move them first")


@pytest.fixture
def client(monkeypatch):
    stub = _Stub()
    for name in ("overview", "detail", "create", "update", "delete"):
        monkeypatch.setattr(api.service, name, getattr(stub, name))

    async def get_pool(session, pool_id):
        return stub.pools.get(pool_id)
    monkeypatch.setattr(api.repo, "get_pool", get_pool)

    audit_calls: list[dict] = []

    async def record(session, **kw):
        audit_calls.append(kw)
    monkeypatch.setattr(api, "audit", types.SimpleNamespace(
        record=record, client_of=lambda r: ("127.0.0.1", "test"),
        actor_of=lambda p: p.username))

    app = create_app()

    async def session_override():
        yield _Session()
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[current_principal] = lambda: Principal("tester", "admin")

    tc = TestClient(app, raise_server_exceptions=False)
    tc.stub, tc.audit, tc.app_ = stub, audit_calls, app
    return tc


def _as(client, role):
    client.app_.dependency_overrides[current_principal] = lambda: Principal("who", role)


def test_list_is_open_to_any_authenticated_user(client):
    _as(client, "viewer")
    r = client.get("/api/v1/pools")
    assert r.status_code == 200
    assert r.json()["pools"][0]["id"] == "p1"
    assert r.json()["planes"] == ["bms"]


def test_create_needs_admin(client):
    _as(client, "operator")
    r = client.post("/api/v1/pools", json={"name": "x", "datacenter_id": "dc-1", "plane": "bms"})
    assert r.status_code == 403
    assert client.stub.calls == []


def test_create_returns_201_and_the_pool_and_audits_it(client):
    r = client.post("/api/v1/pools", json={
        "name": "DC1/BMS", "datacenter_id": "dc-1", "plane": "bms",
        "trap_vip": "10.52.1.250",
        "bbmd_settings": {"enabled": True, "bbmd": "10.52.1.1:47808", "ttl_s": 120}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "p-new" and body["trap_vip"] == "10.52.1.250"
    assert client.stub.calls[0][0] == "create"
    assert client.audit[0]["action"] == "pool.create"
    assert client.audit[0]["after"]["bbmd_settings"]["bbmd"] == "10.52.1.1:47808"


def test_create_rejects_an_unknown_plane_at_the_schema(client):
    r = client.post("/api/v1/pools", json={"name": "x", "datacenter_id": "dc-1",
                                          "plane": "provider"})
    assert r.status_code == 422
    assert client.stub.calls == []


def test_create_conflict_is_409_and_a_bad_value_is_422(client):
    assert client.post("/api/v1/pools", json={"name": "dup", "datacenter_id": "dc-1",
                                             "plane": "bms"}).status_code == 409
    r = client.post("/api/v1/pools", json={"name": "bad", "datacenter_id": "dc-1",
                                          "plane": "bms"})
    assert r.status_code == 422
    assert "not an IP address" in r.json()["detail"]


def test_get_unknown_pool_is_404(client):
    assert client.get("/api/v1/pools/nope").status_code == 404
    assert client.get("/api/v1/pools/p1").json()["ranges"][0]["cidr"] == "10.52.1.0/24"


def test_patch_reports_what_changed_and_audits_before_and_after(client):
    r = client.patch("/api/v1/pools/p1", json={"trap_vip": "10.52.1.250"})
    assert r.status_code == 200
    assert r.json() == {"id": "p1", "changed": {"trap_vip": "10.52.1.250"}}
    assert client.audit[0]["action"] == "pool.update"
    assert client.audit[0]["before"] == {"trap_vip": None}


def test_patch_with_nothing_effective_neither_commits_nor_audits(client):
    r = client.patch("/api/v1/pools/p1", json={})
    assert r.status_code == 200 and r.json()["changed"] == {}
    assert client.audit == []


def test_patch_cannot_move_site_or_plane_at_the_schema(client):
    r = client.patch("/api/v1/pools/p1", json={"plane": "it_oob"})
    assert r.status_code == 422  # extra = forbid


def test_delete_is_204_when_empty_and_422_when_collectors_are_placed(client):
    client.stub.placed = 2
    r = client.delete("/api/v1/pools/p1")
    assert r.status_code == 422
    assert "2 collector(s)" in r.json()["detail"]

    client.stub.placed = 0
    r = client.delete("/api/v1/pools/p1")
    assert r.status_code == 204
    assert client.audit[-1]["action"] == "pool.delete"
    assert client.audit[-1]["before"]["name"] == "DC1/BMS"


def test_firewall_matrix_json_and_text(client):
    r = client.get("/api/v1/pools/p1/firewall-matrix")
    assert r.status_code == 200
    body = r.json()
    assert body["pool"] == "DC1/BMS"
    assert any(row["protocol"] == "core" for row in body["rows"])

    r = client.get("/api/v1/pools/p1/firewall-matrix?format=text")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text.startswith("Firewall matrix - pool DC1/BMS")


def test_firewall_matrix_for_an_unknown_pool_is_404(client):
    assert client.get("/api/v1/pools/nope/firewall-matrix").status_code == 404
