"""The HTTP half: auth, error shaping, and what never reaches a log.

Driven through httpx's own MockTransport rather than a hand-rolled double, so
the request that Jira would have received is the request under test - headers,
encoding and all.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from app.integrations.jira.client import JiraClient, JiraError, _redact


def client_for(handler, *, secret=None, kind="api_token"):
    transport = httpx.MockTransport(handler)
    return JiraClient(
        base_url="https://acme.atlassian.net",
        secret=secret or {"username": "svc@acme.com", "token": "s3cr3t"},
        secret_kind=kind,
        client=httpx.AsyncClient(transport=transport))


# -------------------------------------------------------------------- auth

async def test_a_cloud_token_is_sent_as_basic_with_the_account_email():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True})

    c = client_for(handler)
    await c.get("/rest/api/3/myself")
    expected = base64.b64encode(b"svc@acme.com:s3cr3t").decode()
    assert seen["auth"] == f"Basic {expected}"


async def test_a_data_center_token_is_sent_as_bearer():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={})

    c = client_for(handler, secret={"token": "pat-abc"}, kind="pat")
    await c.get("/rest/api/3/myself")
    assert seen["auth"] == "Bearer pat-abc"


def test_a_cloud_credential_without_an_email_is_refused_at_construction():
    """Not at first use. A Basic header with an empty username authenticates
    as nobody and comes back 401, which reads as a wrong password."""
    with pytest.raises(JiraError, match="account email"):
        client_for(lambda r: httpx.Response(200), secret={"token": "x"})


def test_an_unknown_credential_kind_is_refused():
    with pytest.raises(JiraError, match="unsupported credential kind"):
        client_for(lambda r: httpx.Response(200), kind="oauth2")


# ------------------------------------------------------------------- 429

async def test_a_429_carries_its_retry_after():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "120",
                                            "RateLimit-Reason": "jira-burst-based"},
                              json={"errorMessages": ["slow down"]})

    c = client_for(handler)
    with pytest.raises(JiraError) as caught:
        await c.post("/rest/api/3/issue", json_body={})
    assert caught.value.status == 429
    assert caught.value.retry_after == 120.0
    assert caught.value.retryable


async def test_a_429_without_a_header_still_retries_on_the_schedule():
    c = client_for(lambda r: httpx.Response(429, json={}))
    with pytest.raises(JiraError) as caught:
        await c.post("/rest/api/3/issue", json_body={})
    assert caught.value.retry_after is None and caught.value.retryable


# ------------------------------------------------------------------- 400

async def test_a_field_error_survives_into_the_message():
    """The single most common first-run failure, and the one thing that makes
    it fixable: Jira puts it in `errors`, not `errorMessages`."""
    def handler(request):
        return httpx.Response(400, json={
            "errorMessages": [],
            "errors": {"customfield_10101":
                       "Field cannot be set. It is not on the appropriate screen."}})

    c = client_for(handler)
    with pytest.raises(JiraError) as caught:
        await c.post("/rest/api/3/issue", json_body={})
    assert caught.value.status == 400
    assert not caught.value.retryable
    assert "customfield_10101" in str(caught.value)
    assert "not on the appropriate screen" in str(caught.value)


async def test_a_long_non_jira_body_is_clipped():
    """An SSO login page answering instead of Jira is a whole HTML document,
    and it would land in `outbox.last_error` and on a settings page."""
    c = client_for(lambda r: httpx.Response(502, text="<html>" + "x" * 9000))
    with pytest.raises(JiraError) as caught:
        await c.get("/rest/api/3/myself")
    assert len(caught.value.body) <= 600


# -------------------------------------------------------------- responses

async def test_a_204_decodes_to_none():
    """The documented success for a transition. Treating an empty body as an
    error would make every successful transition look like a failure."""
    c = client_for(lambda r: httpx.Response(204))
    assert await c.post("/rest/api/3/issue/DCOPS-1/transitions",
                        json_body={}) is None


async def test_a_200_that_is_not_json_is_a_failure_not_a_none():
    """None would look like a successful transition."""
    c = client_for(lambda r: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(JiraError, match="not a login portal"):
        await c.get("/rest/api/3/myself")


async def test_a_transport_failure_has_no_status_and_always_retries():
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    c = client_for(handler)
    with pytest.raises(JiraError) as caught:
        await c.get("/rest/api/3/myself")
    assert caught.value.status is None and caught.value.retryable


# -------------------------------------------------------------- mechanics

async def test_an_attachment_carries_the_xsrf_header():
    """Attachment uploads are rejected outright without it."""
    seen = {}

    def handler(request):
        seen["token"] = request.headers.get("X-Atlassian-Token")
        return httpx.Response(200, json=[])

    c = client_for(handler)
    await c.post("/rest/api/3/issue/DCOPS-1/attachments",
                 files={"file": ("a.json", b"{}")})
    assert seen["token"] == "no-check"


async def test_the_limiter_is_asked_for_the_issue_key():
    """Jira limits writes against a SINGLE issue independently of the
    per-second endpoint limit, so the bucket has to be per key."""
    asked = []

    class Limiter:
        async def acquire(self, issue_key=None):
            asked.append(issue_key)
            return 0.0

    c = client_for(lambda r: httpx.Response(200, json={}))
    c._limiter = Limiter()
    await c.post("/rest/api/3/issue/DCOPS-1/comment", json_body={},
                 issue_key="DCOPS-1")
    assert asked == ["DCOPS-1"]


# -------------------------------------------------------------- redaction

def test_a_credential_is_stripped_out_of_an_error_string():
    """Belt and braces - nothing should be putting a header in a message -
    but an error string here lands in a database column, a log line and a
    settings page."""
    token = base64.b64encode(b"svc@acme.com:s3cr3t").decode()
    assert "s3cr3t" not in _redact(f"401 for Basic {token}")
    assert "[redacted]" in _redact("Authorization: Bearer pat-abc")


def test_redaction_terminates():
    """The obvious implementation - replace 'Bearer ' in a loop - never
    terminates, because the replacement contains what it searches for."""
    assert _redact("Bearer a Bearer b Bearer c") \
        == "Bearer [redacted] Bearer [redacted] Bearer [redacted]"
