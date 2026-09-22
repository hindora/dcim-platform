"""Credentials in, connection proved.

Two halves. The first is the one that matters at 3am in six months: a
credential is encrypted at rest with the same AES-256-GCM machinery as device
credentials, and the only thing any API may ever return about it is a hint
that says what KIND of secret exists and how long it is.

The second is the connection test, which is the feature. Everything that goes
wrong with a Jira integration goes wrong at configuration time and surfaces
hours later as silence, so each question is asked up front and reported as its
own line - "authentication works but the project key is wrong" and "the
credential is wrong" need completely different fixes.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.security import decrypt_secret
from app.integrations.jira.client import JiraClient
from app.services import integrations as service

# ---------------------------------------------------------------- secrets

def test_a_cloud_credential_round_trips_through_encryption():
    blob, _hint, kind = service.build_secret(
        "jira_cloud", {"username": "svc@acme.com", "token": "s3cr3t-token"})
    assert kind == "api_token"
    assert decrypt_secret(blob) == {"username": "svc@acme.com",
                                    "token": "s3cr3t-token"}


def test_the_hint_never_contains_the_secret():
    """The obvious hint for a credential is the credential. `hint_is_safe` is
    asserted rather than trusted for exactly that reason."""
    _, hint, _ = service.build_secret(
        "jira_cloud", {"username": "svc@acme.com", "token": "s3cr3t-token"})
    assert "s3cr3t-token" not in hint
    assert "svc@acme.com" in hint            # not a secret, and identifies it
    assert "12 chars" in hint                # length is not the value


def test_a_data_center_deployment_takes_a_bearer_token_not_an_email():
    """Cloud has no personal access tokens and Data Center has no API tokens.
    Offering both everywhere only produces configurations that cannot work."""
    blob, _, kind = service.build_secret("jira_dc", {"token": "pat-abc"})
    assert kind == "pat" and decrypt_secret(blob) == {"token": "pat-abc"}


def test_a_cloud_credential_without_an_email_is_refused():
    with pytest.raises(service.IntegrationError, match="email"):
        service.build_secret("jira_cloud", {"token": "x"})


def test_an_empty_token_is_refused():
    with pytest.raises(service.IntegrationError, match="empty"):
        service.build_secret("jira_dc", {"token": "   "})


def test_an_unknown_kind_is_refused():
    with pytest.raises(service.IntegrationError, match="unknown integration kind"):
        service.build_secret("servicenow", {"token": "x"})


def test_webhook_tokens_are_long_and_unique():
    tokens = {service.new_webhook_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 40 for t in tokens)


# --------------------------------------------------------------- base URL

def test_a_credential_is_never_sent_over_plain_http():
    """Basic auth over HTTP puts the credential on the wire in base64, which
    is not encryption."""
    with pytest.raises(service.IntegrationError, match="plain HTTP"):
        service.check_base_url("jira_cloud", "http://jira.acme.com")


def test_localhost_over_http_is_allowed_because_it_is_a_test_double():
    assert service.check_base_url("jira_dc", "http://localhost:8080") \
        == "http://localhost:8080"


def test_a_trailing_slash_is_stripped():
    """Two spellings of one base URL would produce two remote links on one
    issue, because globalId is built from it."""
    assert service.check_base_url("jira_cloud", "https://acme.atlassian.net/") \
        == "https://acme.atlassian.net"


def test_something_that_is_not_a_url_is_refused():
    with pytest.raises(service.IntegrationError, match="must start with https"):
        service.check_base_url("jira_cloud", "acme.atlassian.net")


# ------------------------------------------------------------- enablement

def test_an_integration_with_no_project_cannot_be_enabled():
    assert "project key" in service.ready_to_enable({"config": {}})


def test_half_a_service_desk_configuration_cannot_be_enabled():
    """With only one of the two, requests would be created without a request
    type and would be malformed on the customer portal."""
    why = service.ready_to_enable(
        {"config": {"project_key": "DCOPS", "service_desk_id": "10"}})
    assert "request type" in why


def test_a_complete_configuration_can_be_enabled():
    assert service.ready_to_enable({"config": {"project_key": "DCOPS"}}) is None
    assert service.ready_to_enable({"config": {
        "project_key": "DCOPS", "service_desk_id": "10",
        "request_type_id": "25"}}) is None


# ------------------------------------------------------------ expiry input

def test_an_expiry_is_parsed_from_iso():
    assert service.parse_expiry("2027-03-01").year == 2027


def test_an_unparseable_expiry_is_refused_rather_than_ignored():
    """Silently dropping it would leave the credential with no expiry and no
    warning - the exact silence the column exists to break."""
    with pytest.raises(service.IntegrationError, match="ISO-8601"):
        service.parse_expiry("next march")


def test_no_expiry_is_a_legitimate_answer():
    assert service.parse_expiry(None) is None
    assert service.parse_expiry("") is None


# -------------------------------------------------------- connection test

def _client_for(routes):
    """A JiraClient over MockTransport, injected into `run_test`."""
    def handler(request):
        for path, response in routes.items():
            if request.url.path == path:
                return response
        return httpx.Response(404, json={"errorMessages": ["not found"]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def run(routes, config, monkeypatch):
    client = JiraClient(base_url="https://acme.atlassian.net",
                        secret={"username": "svc@acme.com", "token": "t"},
                        secret_kind="api_token", client=_client_for(routes))
    return await service.run_test(
        {"base_url": "https://acme.atlassian.net", "config": config}, client)


OK_ROUTES = {
    "/rest/api/3/myself": httpx.Response(200, json={"displayName": "DCIM bot"}),
    "/rest/api/3/project/DCOPS": httpx.Response(200, json={
        "name": "DC Operations",
        "issueTypes": [{"name": "Incident"}, {"name": "Task"}]}),
    "/rest/api/3/priority": httpx.Response(200, json=[
        {"name": n} for n in ("Highest", "High", "Medium", "Low", "Lowest")]),
    "/rest/api/3/field": httpx.Response(200, json=[
        {"id": "customfield_10101", "name": "Device", "custom": True},
        {"id": "summary", "name": "Summary", "custom": False}]),
}


async def test_a_good_configuration_passes_every_check(monkeypatch):
    result = await run(OK_ROUTES,
                       {"project_key": "DCOPS", "issue_type": "Incident"},
                       monkeypatch)
    assert result["ok"] is True
    assert result["discovered"]["account"] == "DCIM bot"
    assert result["discovered"]["project_name"] == "DC Operations"


async def test_bad_authentication_stops_before_asking_anything_else(monkeypatch):
    """Every further call would be one more failed authentication against a
    tenant that may be counting them."""
    routes = {**OK_ROUTES,
              "/rest/api/3/myself": httpx.Response(401, json={
                  "errorMessages": ["Client must be authenticated"]})}
    result = await run(routes, {"project_key": "DCOPS"}, monkeypatch)
    assert result["ok"] is False
    assert [c["name"] for c in result["checks"]] == ["Authentication"]


async def test_a_wrong_project_key_is_its_own_line(monkeypatch):
    routes = {**OK_ROUTES,
              "/rest/api/3/project/DCOPS": httpx.Response(404, json={
                  "errorMessages": ["No project could be found"]})}
    result = await run(routes, {"project_key": "DCOPS"}, monkeypatch)
    failed = [c for c in result["checks"] if not c["ok"]]
    assert failed and failed[0]["name"] == "Project DCOPS"
    # Authentication still passed, which is the distinction the operator needs.
    assert result["checks"][0]["ok"] is True


async def test_an_issue_type_the_project_does_not_have_is_reported_with_the_list(
        monkeypatch):
    result = await run(OK_ROUTES,
                       {"project_key": "DCOPS", "issue_type": "Outage"},
                       monkeypatch)
    failed = next(c for c in result["checks"] if not c["ok"])
    assert "Incident" in failed["detail"] and "Task" in failed["detail"]


async def test_a_renamed_priority_is_reported_as_a_degradation_not_a_failure(
        monkeypatch):
    """An unmapped priority is dropped from the create rather than failing it,
    so the ticket still lands - at the project default, which is how a
    CRITICAL ends up sorted below a password reset."""
    routes = {**OK_ROUTES,
              "/rest/api/3/priority": httpx.Response(200, json=[
                  {"name": "P1"}, {"name": "P2"}])}
    result = await run(routes, {"project_key": "DCOPS",
                                "issue_type": "Incident"}, monkeypatch)
    detail = next(c for c in result["checks"]
                  if c["name"] == "Severity mapping")["detail"]
    assert "Highest" in detail and "project default" in detail


async def test_a_custom_field_from_another_site_is_caught_before_it_fails_a_create(
        monkeypatch):
    result = await run(OK_ROUTES, {"project_key": "DCOPS",
                                   "issue_type": "Incident",
                                   "fields": {"device": "customfield_99999"}},
                       monkeypatch)
    failed = next(c for c in result["checks"] if not c["ok"])
    assert failed["name"] == "Mapped fields"
    assert "customfield_99999" in failed["detail"]


async def test_the_discovered_fields_are_offered_for_the_settings_page(
        monkeypatch):
    """So the operator picks from a list instead of typing an id from another
    tenant's documentation."""
    result = await run(OK_ROUTES, {"project_key": "DCOPS",
                                   "issue_type": "Incident"}, monkeypatch)
    assert result["discovered"]["fields"] == [
        {"id": "customfield_10101", "name": "Device"}]


async def test_a_service_desk_request_type_is_checked_too(monkeypatch):
    routes = {**OK_ROUTES,
              "/rest/servicedeskapi/servicedesk/10/requesttype":
                  httpx.Response(200, json={"values": [
                      {"id": 25, "name": "Report an outage"}]}),
              "/rest/servicedeskapi/servicedesk/10/requesttype/25/field":
                  httpx.Response(200, json={"requestTypeFields": [
                      {"fieldId": "summary", "name": "Summary", "required": True},
                      {"fieldId": "description", "name": "What happened",
                       "required": True}]})}
    result = await run(routes, {"project_key": "DCOPS", "issue_type": "Incident",
                                "service_desk_id": "10",
                                "request_type_id": "25"}, monkeypatch)
    assert result["ok"] is True
    assert result["discovered"]["request_types"] == [
        {"id": "25", "name": "Report an outage"}]


async def test_a_required_field_the_integration_cannot_send_is_flagged(
        monkeypatch):
    """The request API accepts only fields the form exposes, so a required
    one we do not send rejects every create - and a required one we are not
    told about is silently dropped, which is worse."""
    routes = {**OK_ROUTES,
              "/rest/servicedeskapi/servicedesk/10/requesttype":
                  httpx.Response(200, json={"values": [{"id": 25, "name": "X"}]}),
              "/rest/servicedeskapi/servicedesk/10/requesttype/25/field":
                  httpx.Response(200, json={"requestTypeFields": [
                      {"fieldId": "customfield_200", "name": "Affected service",
                       "required": True}]})}
    result = await run(routes, {"project_key": "DCOPS", "issue_type": "Incident",
                                "service_desk_id": "10",
                                "request_type_id": "25"}, monkeypatch)
    failed = next(c for c in result["checks"] if not c["ok"])
    assert failed["name"] == "Required request fields"
    assert "Affected service" in failed["detail"]


def test_a_bare_date_is_read_as_utc_not_as_the_servers_timezone():
    """A naive datetime going into a `timestamptz` column is interpreted in the
    database session's timezone. On a server at +05:30 a typed "2026-10-10"
    was stored as 2026-10-09T18:30Z - the expiry moved to the previous day,
    and the alarm that warns 30 days out would have fired a day early for
    ever. Found by reading a real response back, not by a test."""
    parsed = service.parse_expiry("2026-10-10")
    assert parsed.tzinfo is not None
    assert parsed.isoformat() == "2026-10-10T00:00:00+00:00"


def test_an_explicit_zone_is_respected():
    assert service.parse_expiry("2026-10-10T12:00:00+02:00").utcoffset().seconds == 7200
