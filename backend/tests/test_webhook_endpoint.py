"""The one endpoint a stranger can reach.

Exercised through the real ASGI app so that the things which are easy to get
wrong in a framework - a dependency that was supposed to be absent, a body
read twice, a status code that invites Jira to retry forever - are tested as
the framework will actually run them.

The database layer is replaced; the routing, the header parsing and the raw
body are not.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import webhooks as endpoint
from app.db.session import get_session
from app.integrations.jira import webhook
from app.main import create_app

SECRET = b"a-shared-secret"
TOKEN = "a-very-long-callback-token"

PAYLOAD = {"webhookEvent": "jira:issue_updated",
           "issue": {"key": "DCOPS-142", "fields": {}},
           "changelog": {"items": []}}


class Repo:
    def __init__(self):
        self.integration: dict[str, Any] | None = {
            "id": "i1", "name": "Acme Jira", "enabled": True,
            "config": {}, "webhook_blob": b"opaque"}
        self.enqueued: list[dict] = []
        self.fresh = True

    async def integration_by_webhook_token(self, session, token):
        return self.integration if token == TOKEN else None

    async def enqueue_inbound(self, session, **kw):
        self.enqueued.append(kw)
        return self.fresh


class Service:
    secret: bytes | None = SECRET

    async def webhook_secret(self, integration):
        return self.secret


class Session:
    async def commit(self):
        return None


@pytest.fixture
def client(monkeypatch):
    repo, service = Repo(), Service()
    monkeypatch.setattr(endpoint, "repo", repo)
    monkeypatch.setattr(endpoint, "service", service)

    app = create_app()

    async def session_override():
        yield Session()

    app.dependency_overrides[get_session] = session_override
    test_client = TestClient(app, raise_server_exceptions=False)
    test_client.repo, test_client.service = repo, service
    return test_client


def post(client, body: bytes, *, token: str = TOKEN, signature=...):
    if signature is ...:
        signature = webhook.sign(body, SECRET)
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers[webhook.SIGNATURE_HEADER] = signature
    return client.post(f"/api/v1/integrations/jira/webhook/{token}",
                       content=body, headers=headers)


# ------------------------------------------------------------ the happy path

def test_a_signed_delivery_is_accepted_and_queued(client):
    body = json.dumps(PAYLOAD).encode()
    response = post(client, body)
    assert response.status_code == 204
    assert client.repo.enqueued[0]["issue_key"] == "DCOPS-142"
    assert client.repo.enqueued[0]["event"] == "jira:issue_updated"


def test_the_handler_only_queues_and_does_not_interpret(client):
    """Jira gives a receiver about 30 seconds and retries five times on
    anything that is not a 2xx. Correlation, alarm mutation and outbound calls
    all belong on the dispatcher, where a failure is a retry rather than a
    redelivery."""
    post(client, json.dumps(PAYLOAD).encode())
    assert list(client.repo.enqueued[0]) == [
        "integration_id", "event", "issue_key", "payload", "dedup_sha"]


def test_a_redelivery_still_answers_204(client):
    """Anything else invites Jira to keep trying."""
    client.repo.fresh = False
    assert post(client, json.dumps(PAYLOAD).encode()).status_code == 204


# ---------------------------------------------------------------- refusals

def test_no_signature_is_refused(client):
    assert post(client, b"{}", signature=None).status_code == 401
    assert client.repo.enqueued == []


def test_a_signature_from_the_wrong_secret_is_refused(client):
    body = json.dumps(PAYLOAD).encode()
    assert post(client, body,
                signature=webhook.sign(body, b"other")).status_code == 401


def test_a_valid_signature_over_a_different_body_is_refused(client):
    """Replaying one delivery's header against another's body."""
    signature = webhook.sign(b'{"a":1}', SECRET)
    assert post(client, b'{"a":2}', signature=signature).status_code == 401


def test_an_unknown_token_answers_exactly_like_a_bad_signature(client):
    """A 404 here would turn the token into an oracle for which tokens
    exist."""
    body = json.dumps(PAYLOAD).encode()
    unknown = post(client, body, token="nope")
    bad_signature = post(client, body, signature="sha256=00")
    assert unknown.status_code == bad_signature.status_code == 401
    assert unknown.content == bad_signature.content


def test_a_disabled_integration_is_refused(client):
    client.repo.integration = {**client.repo.integration, "enabled": False}
    assert post(client, json.dumps(PAYLOAD).encode()).status_code == 401


def test_an_integration_with_no_webhook_secret_refuses_everything(client):
    """Not accepts everything, which is what an HMAC over b"" would do if the
    guard in `verify` were missing."""
    client.service.secret = None
    assert post(client, json.dumps(PAYLOAD).encode()).status_code == 401


def test_a_signed_body_that_is_not_json_is_a_400(client):
    """Signed by us and still unparseable means something in front of Jira is
    rewriting bodies - worth distinguishing from a forgery."""
    assert post(client, b"not json").status_code == 400


def test_a_signed_json_scalar_is_refused(client):
    assert post(client, b'"hello"').status_code == 400


def test_an_oversized_body_is_refused_before_it_is_parsed(client):
    big = b'{"x":"' + b"a" * (endpoint.MAX_BODY + 10) + b'"}'
    assert post(client, big).status_code == 413
    assert client.repo.enqueued == []


# ------------------------------------------------------------------ wiring

def test_the_endpoint_carries_no_jwt_dependency():
    """The point of keeping this route in its own module. Adding an auth
    dependency here would break every delivery; this asserts nobody has."""
    app = create_app()
    route = next(r for r in _routes(app)
                 if "jira/webhook" in getattr(r, "path", ""))
    names = {d.call.__name__ for d in route.dependant.dependencies
             if getattr(d, "call", None)}
    assert "current_principal" not in names
    assert not any("role" in n for n in names)


def test_the_endpoint_is_not_advertised_in_the_schema():
    """It is not an API anybody but Jira calls, and listing it only helps
    somebody looking for an unauthenticated door."""
    assert not any("jira/webhook" in p for p in create_app().openapi()["paths"])


def _routes(app):
    """Every leaf route, whatever shape the framework nests them in.

    This FastAPI version wraps an included router in a `_IncludedRouter`
    holding `original_router`, rather than flattening the paths onto the app -
    so walking `app.routes` alone finds five routes and none of ours.
    """
    out, stack = [], list(app.routes)
    while stack:
        route = stack.pop()
        nested = (getattr(route, "routes", None)
                  or getattr(getattr(route, "original_router", None),
                             "routes", None))
        if nested:
            stack.extend(nested)
        else:
            out.append(route)
    return out
