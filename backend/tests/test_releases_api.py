"""docs/26 Phase 7 over HTTP: signed release publish, the command long-poll,
and route ordering. Real Ed25519 keys; stubbed persistence."""

from __future__ import annotations

import base64
import hashlib
import types

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from app.api.v1 import collector as collector_api
from app.api.v1 import releases as api
from app.core.config import get_settings
from app.core.security import Principal, current_principal, require_collector
from app.db.session import get_session
from app.main import create_app

KEY = Ed25519PrivateKey.generate()
PUB = base64.b64encode(KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
BINARY = b"\x7fELF fake collector build"
SHA = hashlib.sha256(BINARY).hexdigest()
SIG = base64.b64encode(KEY.sign(bytes.fromhex(SHA))).decode()


def test_a_signature_verifies_only_for_its_own_digest_and_key():
    assert api.verify_signature(SHA, SIG, PUB)
    assert not api.verify_signature(hashlib.sha256(b"tampered").hexdigest(), SIG, PUB)
    other = base64.b64encode(Ed25519PrivateKey.generate().public_key()
                             .public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    assert not api.verify_signature(SHA, SIG, other)


class _Session:
    async def commit(self):
        return None

    async def scalar(self, *_a, **_k):
        return "active"


@pytest.fixture
def client(monkeypatch, tmp_path):
    stored: dict = {}

    async def add_release(session, r):
        if r["version"] in stored:
            return False
        stored[r["version"]] = r
        return True

    async def record(session, **kw):
        return None

    monkeypatch.setattr(api.repo, "add_release", add_release)
    monkeypatch.setattr(api, "audit", types.SimpleNamespace(
        record=record, client_of=lambda r: ("127.0.0.1", "t"), actor_of=lambda p: p.username))
    settings = get_settings().model_copy(update={
        "release_dir": str(tmp_path), "release_trusted_keys": {"k1": PUB}})
    app = create_app()

    async def session_override():
        yield _Session()
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[current_principal] = lambda: Principal("tester", "admin")
    app.dependency_overrides[require_collector] = lambda: "col-1"
    tc = TestClient(app, raise_server_exceptions=False)
    tc.stored, tc.tmp = stored, tmp_path
    return tc


def _publish(client, sig=SIG, key_id="k1", version="2.0.0"):
    return client.post("/api/v1/collectors/releases",
                       data={"version": version, "signature": sig, "key_id": key_id},
                       files={"artifact": ("collector", BINARY, "application/octet-stream")})


def test_a_correctly_signed_release_is_stored_with_its_digest(client):
    r = _publish(client)
    assert r.status_code == 201, r.text
    assert r.json()["sha256"] == SHA and r.json()["verified_by_platform"] is True
    assert (client.tmp / "2.0.0" / "collector").read_bytes() == BINARY


def test_a_bad_signature_or_unknown_key_is_refused(client):
    bad = base64.b64encode(KEY.sign(b"something else")).decode()
    assert _publish(client, sig=bad).status_code == 422
    assert _publish(client, key_id="nope").status_code == 422
    assert client.stored == {}


def test_publishing_the_same_version_twice_is_409(client):
    assert _publish(client).status_code == 201
    assert _publish(client).status_code == 409


def test_releases_is_not_read_as_a_collector_id(client, monkeypatch):
    async def releases(session):
        return []
    monkeypatch.setattr(api.repo, "releases", releases)
    assert client.get("/api/v1/collectors/releases").json() == {"releases": []}


# ------------------------------------------------------------- the long-poll

class _FakeSessionmaker:
    def __call__(self):
        return self

    async def __aenter__(self):
        return _Session()

    async def __aexit__(self, *a):
        return None


def _poll_stubs(monkeypatch, commands, tokens):
    from app.db import session as session_mod
    from app.repositories import commands as cmd_repo

    calls = {"n": 0}

    async def claim(s, cid):
        return commands.pop(0) if commands else []

    async def token(s):
        calls["n"] += 1
        return tokens[min(calls["n"] - 1, len(tokens) - 1)]

    monkeypatch.setattr(cmd_repo, "claim_pending", claim)
    monkeypatch.setattr(cmd_repo, "moves_token", token)
    monkeypatch.setattr(session_mod, "get_sessionmaker", lambda: _FakeSessionmaker())
    return calls


def test_a_pending_command_returns_at_once(client, monkeypatch):
    _poll_stubs(monkeypatch, [[{"id": "c1", "kind": "upgrade", "payload": {"v": 1},
                                "created_at": None}]], ["t1"])
    r = client.get("/api/v1/collector/commands", params={"collector_id": "col-1",
                                                          "wait": 25, "moves": "t1"})
    assert r.status_code == 200
    assert r.json() == {"commands": [{"id": "c1", "kind": "upgrade", "payload": {"v": 1}}],
                        "moves": "t1"}


def test_an_assignment_change_returns_without_a_command(client, monkeypatch):
    calls = _poll_stubs(monkeypatch, [], ["t1", "t1", "t2"])
    r = client.get("/api/v1/collector/commands", params={"collector_id": "col-1",
                                                          "wait": 25, "moves": "t1"})
    assert r.json() == {"commands": [], "moves": "t2"} and calls["n"] == 3


def test_nothing_to_say_times_out_empty(client, monkeypatch):
    _poll_stubs(monkeypatch, [], ["t1"])
    r = client.get("/api/v1/collector/commands", params={"collector_id": "col-1",
                                                          "wait": 0, "moves": "t1"})
    assert r.json() == {"commands": [], "moves": "t1"}


def test_another_collectors_queue_is_forbidden(client, monkeypatch):
    _poll_stubs(monkeypatch, [], ["t1"])
    r = client.get("/api/v1/collector/commands", params={"collector_id": "col-2", "wait": 0})
    assert r.status_code == 403
    assert collector_api is not None
