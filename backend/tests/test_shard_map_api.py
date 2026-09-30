"""Shard map and drain over real HTTP via TestClient, against stubbed
services - the HTTP contract only: route order, auth, the 409 guard, and
the order a drain does things in. Logic is test_shard_map.py (pure) and
test_pools_live.py (real database)."""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import collectors as api
from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.main import create_app


class _Session:
    async def commit(self):
        return None


PREVIEW = {"collector_id": "c1", "owned": 12, "moving": 10, "destinations": {"c2": 10},
           "stranded": 0, "stranded_pools": {}, "pinned": 2, "blockers": [],
           "can_drain": True}


@pytest.fixture
def client(monkeypatch):
    log: list[tuple] = []
    state = {"c1": "active", "c9": "pending"}
    preview = dict(PREVIEW)

    async def collector_state(session, cid):
        return {"id": cid, "state": state[cid]} if cid in state else None

    async def set_state(session, cid, new, actor):
        log.append(("set_state", cid, new))
        state[cid] = new

    async def unpin_all(session, cid):
        log.append(("unpin", cid))
        return 2

    async def run(session):
        log.append(("assigner",))
        return types.SimpleNamespace(ran=True, moved=10)

    async def drain_preview(session, cid):
        return dict(preview)

    async def remaining(session, cid):
        return 2

    async def shard_map(session, **kw):
        log.append(("shard_map", kw))
        return {"summary": {}, "total": 0, "items": []}

    async def history(session, eid):
        return [{"from_collector": "c1", "to_collector": "c2", "epoch": 2,
                 "reason": "drain", "at": None}]

    monkeypatch.setattr(api.fleet_repo, "collector_state", collector_state)
    monkeypatch.setattr(api.fleet_repo, "set_state", set_state)
    monkeypatch.setattr(api.fleet_repo, "unpin_all", unpin_all)
    monkeypatch.setattr(api.fleet_repo, "assignment_history", history)
    monkeypatch.setattr(api.assigner, "run", run)
    monkeypatch.setattr(api.shard_map, "drain_preview", drain_preview)
    monkeypatch.setattr(api.shard_map, "remaining", remaining)
    monkeypatch.setattr(api.shard_map, "shard_map", shard_map)
    monkeypatch.setattr(api, "forget_collector", lambda cid: None)

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
    tc.log, tc.audit, tc.app_, tc.preview, tc.state = log, audit_calls, app, preview, state
    return tc


def _as(client, role):
    client.app_.dependency_overrides[current_principal] = lambda: Principal("who", role)


def test_shard_map_is_not_read_as_a_collector_called_shard_map(client):
    _as(client, "viewer")
    r = client.get("/api/v1/collectors/shard-map?protocol=snmp&limit=25&offset=50")
    assert r.status_code == 200
    kw = client.log[-1][1]
    assert kw["protocol"] == "snmp" and kw["limit"] == 25 and kw["offset"] == 50


def test_history_rejects_a_malformed_id_as_404(client):
    assert client.get("/api/v1/collectors/shard-map/not-a-uuid/history").status_code == 404
    r = client.get("/api/v1/collectors/shard-map/"
                   "00000000-0000-0000-0000-000000000001/history")
    assert r.status_code == 200 and r.json()["moves"][0]["reason"] == "drain"


def test_preview_is_readable_by_anyone_signed_in(client):
    _as(client, "viewer")
    r = client.get("/api/v1/collectors/c1/drain-preview")
    assert r.status_code == 200 and r.json()["moving"] == 10
    assert r.json()["state"] == "active"
    assert client.get("/api/v1/collectors/nope/drain-preview").status_code == 404


def test_drain_needs_admin(client):
    _as(client, "operator")
    assert client.post("/api/v1/collectors/c1/drain", json={}).status_code == 403
    assert client.log == []


def test_drain_marks_draining_then_runs_the_assigner_and_audits(client):
    r = client.post("/api/v1/collectors/c1/drain", json={})
    assert r.status_code == 200
    assert client.log == [("set_state", "c1", "draining"), ("assigner",)]
    assert r.json()["recorded_moves"] == 10 and r.json()["unpinned"] == 0
    assert client.audit[0]["action"] == "collector.drain"
    assert client.audit[0]["after"]["moving"] == 10


def test_release_pins_unpins_before_the_state_changes(client):
    r = client.post("/api/v1/collectors/c1/drain", json={"release_pins": True})
    assert r.status_code == 200 and r.json()["unpinned"] == 2
    assert client.log[0] == ("unpin", "c1")


def test_a_drain_that_would_strand_endpoints_is_409_unless_forced(client):
    client.preview.update(stranded=3, can_drain=False,
                          blockers=["3 endpoint(s) would have nowhere to go"])
    r = client.post("/api/v1/collectors/c1/drain", json={})
    assert r.status_code == 409 and "nowhere to go" in r.json()["detail"]
    assert client.log == [], "a refused drain changes nothing"
    r = client.post("/api/v1/collectors/c1/drain", json={"force": True})
    assert r.status_code == 200
    assert client.audit[0]["after"]["forced"] is True


def test_a_pending_collector_has_nothing_to_drain(client):
    assert client.post("/api/v1/collectors/c9/drain", json={}).status_code == 409


def test_undrain_only_from_draining(client):
    assert client.post("/api/v1/collectors/c1/undrain").status_code == 409
    client.state["c1"] = "draining"
    r = client.post("/api/v1/collectors/c1/undrain")
    assert r.status_code == 200
    assert client.log == [("set_state", "c1", "active"), ("assigner",)]
    assert client.audit[0]["action"] == "collector.undrain"


def test_unknown_body_keys_are_refused(client):
    assert client.post("/api/v1/collectors/c1/drain",
                       json={"release_pin": True}).status_code == 422
