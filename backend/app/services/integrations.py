"""Integration lifecycle: secrets in, connection proved, configuration cached.

THE CONNECTION TEST IS THE FEATURE. Everything that goes wrong with a Jira
integration goes wrong at configuration time and shows up hours later as
silence: a project key that is a name rather than a key, an issue type that
does not exist on that project's screen, a custom field id copied from a
different site, a service desk id confused with a project id, a priority the
customer renamed. Each of those produces a 400 on the first real alarm, at
which point the alarm is in a dead letter and nobody is watching.

So the test asks Jira every one of those questions up front and reports each
as its own line rather than as one pass/fail - "authentication works but the
project key is wrong" and "the credential is wrong" need completely different
fixes, and a single boolean tells the operator neither. It also returns what
it discovered (issue types, priorities, custom fields, request types) so the
settings page can offer real choices instead of a free-text box.

It is READ-ONLY. It does not create a scratch issue: a test that leaves
rubbish on somebody's service desk gets run once and then avoided, and the
one question it could answer that way - whether this tenant's service desk
API wants ADF or plain text - is a setting with a sane default instead.
"""

from __future__ import annotations

import secrets as pysecrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import (
    credential_hint,
    decrypt_secret,
    encrypt_secret,
    hint_is_safe,
)
from app.integrations.config import IntegrationConfigError, resolved, validate
from app.integrations.jira.client import JiraClient, JiraError
from app.integrations.jira.target import API, SERVICEDESK, IssueTarget
from app.repositories import integrations as repo

log = get_logger("integrations.service")

KINDS = ("jira_cloud", "jira_dc", "jsm_ops")

#: Which credential each deployment kind takes. Not a free choice: Cloud has no
#: personal access tokens and Data Center has no API tokens, so offering both
#: everywhere would only produce configurations that cannot work.
SECRET_KIND_FOR = {"jira_cloud": "api_token", "jira_dc": "pat",
                   "jsm_ops": "api_token"}


class IntegrationError(ValueError):
    """A rejected integration, with a message written for the operator."""


# ---------------------------------------------------------------- secrets

def build_secret(kind: str, payload: dict[str, Any]) -> tuple[bytes, str, str]:
    """Validate, encrypt, and describe a credential.

    Returns (ciphertext, hint, secret_kind). The hint is the only part any API
    is ever allowed to return, and `hint_is_safe` is asserted here rather than
    trusted, because the obvious hint for a credential is the credential.
    """
    secret_kind = SECRET_KIND_FOR.get(kind)
    if secret_kind is None:
        raise IntegrationError(f"unknown integration kind {kind!r}")

    if secret_kind == "api_token":
        email = (payload.get("username") or payload.get("email") or "").strip()
        token = (payload.get("token") or payload.get("password") or "").strip()
        if not email or "@" not in email:
            raise IntegrationError(
                "a Cloud API token is used with the Atlassian account's email "
                "address as the username")
        if not token:
            raise IntegrationError("the API token is empty")
        clean = {"username": email, "token": token}
    else:
        token = (payload.get("token") or "").strip()
        if not token:
            raise IntegrationError("the personal access token is empty")
        clean = {"token": token}

    hint = credential_hint(secret_kind, clean)
    if not hint_is_safe(hint, clean):
        # Never reached with the hint builder above; asserted because the one
        # time it is reached, the alternative is a credential in a column
        # nobody thinks of as sensitive.
        raise IntegrationError("the credential hint would contain the secret")
    return encrypt_secret(clean), hint, secret_kind


def new_webhook_token() -> str:
    """A high-entropy path segment for the inbound callback URL.

    Not the security boundary - the HMAC signature is - but it keeps unsigned
    probes from reaching the verification code at all, and it means a leaked
    URL alone still cannot post a forged transition.
    """
    return pysecrets.token_urlsafe(32)


# ------------------------------------------------------------- validation

def check_base_url(kind: str, base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise IntegrationError("the base URL must start with https://")
    if url.startswith("http://") and not url.startswith("http://localhost"):
        # Basic auth over plain HTTP puts the credential on the wire in
        # base64, which is not encryption. Localhost is allowed because that
        # is a test double, not a tenant.
        raise IntegrationError(
            "refusing to send a credential over plain HTTP; use https")
    if kind == "jira_cloud" and not url.endswith(".atlassian.net"):
        # A warning shaped as a refusal, because the usual cause is a Data
        # Center instance configured as Cloud, and that combination fails
        # later with a 401 that reads like a wrong password.
        log.info("cloud integration on a non-atlassian.net host", url=url)
    return url


def prepare_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    try:
        return validate(raw or {})
    except IntegrationConfigError as exc:
        raise IntegrationError(str(exc)) from exc


def ready_to_enable(integration: dict[str, Any]) -> str | None:
    """Why this integration cannot be turned on yet, or None.

    Checked at enable time rather than at save time: a half-configured row is
    a legitimate state to be in while somebody is still filling the form, and
    refusing to save it makes the form unusable.
    """
    cfg = resolved(integration.get("config"))
    if integration.get("kind") == "jsm_ops":
        # An Operations integration wants none of the issue settings: there is
        # no project, no issue type and no request type, because an alert is
        # not an issue. What it does need is the cloud id, because the alert
        # API is addressed by it rather than by the site URL.
        if not integration.get("cloud_id"):
            return ("it has no cloud id - the Operations alert API is "
                    "addressed by it rather than by the site URL")
        return None
    if not cfg.get("project_key"):
        return "it has no project key"
    if bool(cfg.get("service_desk_id")) != bool(cfg.get("request_type_id")):
        return ("a service desk needs BOTH the service desk id and the request "
                "type id; with only one, tickets would be created without a "
                "request type and would be malformed on the customer portal")
    return None


# ------------------------------------------------------ credential loading
#
# THE ONE PLACE THAT DECRYPTS. `test_secret_access` asserts that no request
# handler ever calls `decrypt_secret`, for the reason it states: in a handler
# the result is one `return` away from the wire. Decryption belongs here, in
# the service, exactly as it does for the collector's assignment chain - and
# the dispatcher goes through here too rather than growing a second copy.


async def build_client(session: AsyncSession, integration: dict[str, Any]
                       ) -> JiraClient:
    """An authenticated client for one integration.

    Raises `JiraError` when the row has no usable credential, which the
    dispatcher treats as a retryable failure and the test endpoint reports as
    a failed authentication check.
    """
    secrets = await repo.secrets_of(session, integration["id"])
    if secrets is None or not secrets.get("blob"):
        raise JiraError("the integration has no stored credential")
    payload = decrypt_secret(bytes(secrets["blob"]))
    return JiraClient(base_url=integration["base_url"], secret=payload,
                      secret_kind=secrets["kind"])


async def build_target(session: AsyncSession, integration: dict[str, Any], *,
                       limiter: Any = None) -> tuple[JiraClient, Any]:
    """The client plus whichever lifecycle this integration's kind wants.

    TWO TARGETS, ONE SPINE. `jsm_ops` pages somebody; the others open a ticket.
    They share the outbox, the policy, the fingerprint, the rate limiter and
    the retry schedule - everything operational - and differ only in what they
    do with an alarm once it arrives. Choosing here rather than in the
    dispatcher keeps that choice in one place.
    """
    client = await build_client(session, integration)
    client._limiter = limiter
    cfg = resolved(integration.get("config"))
    dcim_base = get_settings().public_base_url or None

    if integration["kind"] == "jsm_ops":
        from app.integrations.ops.target import OpsAlertTarget
        if not integration.get("cloud_id"):
            raise JiraError(
                "an Operations integration needs the site's cloud id - the "
                "alert API is addressed by it rather than by the site URL")
        return client, OpsAlertTarget(client, cfg,
                                      cloud_id=integration["cloud_id"],
                                      dcim_base=dcim_base)

    target = IssueTarget(client, cfg, base_url=integration["base_url"],
                         dcim_base=dcim_base)
    return client, target


async def test_connection(session: AsyncSession, integration: dict[str, Any]
                          ) -> dict[str, Any]:
    """The endpoint's entry point, so the handler never holds a credential."""
    try:
        client = await build_client(session, integration)
    except JiraError as exc:
        return {"ok": False,
                "checks": [{"name": "Credential", "ok": False,
                            "detail": str(exc)}],
                "discovered": {}}
    return await run_test(integration, client)


# -------------------------------------------------------- connection test

async def run_test(integration: dict[str, Any], client: JiraClient
                   ) -> dict[str, Any]:
    """Ask Jira every question that would otherwise fail on the first alarm.

    Returns ``{"ok": bool, "checks": [...], "discovered": {...}}``. `checks`
    is a list rather than a verdict because "authentication works but the
    project key is wrong" and "the credential is wrong" need completely
    different fixes, and a single boolean tells the operator neither.
    """
    cfg = resolved(integration.get("config"))
    checks: list[dict[str, Any]] = []
    discovered: dict[str, Any] = {}

    try:
        me = await _check(checks, "Authentication",
                          client.get(f"{API}/myself"))
        if me is None:
            # Nothing else can succeed and every further call would be one
            # more failed authentication against a tenant that may be
            # counting them.
            return {"ok": False, "checks": checks, "discovered": discovered}
        discovered["account"] = me.get("displayName") or me.get("emailAddress")

        project_key = cfg.get("project_key")
        if project_key:
            project = await _check(checks, f"Project {project_key}",
                                   client.get(f"{API}/project/{project_key}"))
            if project:
                discovered["project_name"] = project.get("name")
                discovered["issue_types"] = sorted(
                    t.get("name") for t in project.get("issueTypes") or []
                    if t.get("name"))
                wanted = cfg.get("issue_type")
                if wanted and discovered["issue_types"] \
                        and wanted not in discovered["issue_types"]:
                    checks.append({
                        "name": f"Issue type {wanted}", "ok": False,
                        "detail": ("not available on this project; it has "
                                   + ", ".join(discovered["issue_types"]))})
                else:
                    checks.append({"name": f"Issue type {wanted}", "ok": True,
                                   "detail": ""})
        else:
            checks.append({"name": "Project", "ok": False,
                           "detail": "no project key is configured"})

        priorities = await _check(checks, "Priorities",
                                  client.get(f"{API}/priority"))
        if priorities is not None:
            names = {p.get("name") for p in priorities if p.get("name")}
            discovered["priorities"] = sorted(n for n in names if n)
            missing = sorted(set(cfg["priority_map"].values()) - names)
            if missing:
                # Not fatal. An unmapped priority is dropped from the create
                # rather than failing it, so the ticket still lands - but at
                # the project's default priority, which is how a CRITICAL ends
                # up in a queue sorted below a password reset.
                checks.append({
                    "name": "Severity mapping", "ok": False,
                    "detail": (f"this site has no priority called "
                               f"{', '.join(missing)}; those severities will "
                               f"use the project default")})
            else:
                checks.append({"name": "Severity mapping", "ok": True,
                               "detail": ""})

        fields = await _check(checks, "Custom fields",
                              client.get(f"{API}/field"))
        if fields is not None:
            discovered["fields"] = [
                {"id": f.get("id"), "name": f.get("name")}
                for f in fields
                if f.get("custom") and str(f.get("id", "")).startswith(
                    "customfield_")]
            configured = {v for v in cfg["fields"].values() if v}
            known = {f["id"] for f in discovered["fields"]}
            unknown = sorted(configured - known)
            if unknown:
                checks.append({
                    "name": "Mapped fields", "ok": False,
                    "detail": (f"{', '.join(unknown)} does not exist on this "
                               f"site. A create naming a field that is not on "
                               f"the project's screen fails entirely")})

        if cfg.get("service_desk_id"):
            await _check_servicedesk(checks, discovered, client, cfg)

        await _check_assets(discovered, client)
    finally:
        await client.aclose()

    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "discovered": discovered}


async def _check_servicedesk(checks: list[dict[str, Any]],
                             discovered: dict[str, Any], client: JiraClient,
                             cfg: dict[str, Any]) -> None:
    desk_id = cfg["service_desk_id"]
    types = await _check(
        checks, f"Service desk {desk_id}",
        client.get(f"{SERVICEDESK}/servicedesk/{desk_id}/requesttype"))
    if types is None:
        return
    values = types.get("values") or []
    discovered["request_types"] = [
        {"id": str(t.get("id")), "name": t.get("name")} for t in values]
    wanted = str(cfg.get("request_type_id") or "")
    if wanted and wanted not in {str(t.get("id")) for t in values}:
        checks.append({
            "name": f"Request type {wanted}", "ok": False,
            "detail": ("not on this service desk. A request created without a "
                       "valid request type is malformed on the portal")})
        return
    checks.append({"name": f"Request type {wanted}", "ok": True, "detail": ""})

    # What the request type will REFUSE to be told. The request API only
    # accepts fields the form exposes, so a field mapped here that is not on
    # the form is silently dropped rather than rejected - which is worse,
    # because the ticket arrives looking complete.
    fields = await _check(
        checks, "Request type fields",
        client.get(f"{SERVICEDESK}/servicedesk/{desk_id}/requesttype/"
                   f"{wanted}/field"))
    if fields is not None:
        discovered["request_fields"] = [
            {"id": f.get("fieldId"), "name": f.get("name"),
             "required": bool(f.get("required"))}
            for f in (fields.get("requestTypeFields") or [])]
        required = [f["name"] for f in discovered["request_fields"]
                    if f["required"] and f["id"] not in
                    ("summary", "description")]
        if required:
            checks.append({
                "name": "Required request fields", "ok": False,
                "detail": (f"this request type requires {', '.join(required)}, "
                           f"which this integration does not send; creates "
                           f"will be rejected until the form is relaxed or "
                           f"those fields are mapped")})


async def _check_assets(discovered: dict[str, Any],
                        client: JiraClient) -> None:
    """Is there an Assets workspace on this site?

    Reported as a CAPABILITY rather than as a check that can fail, because it
    cannot: Assets needs JSM Premium or Enterprise, and a site on Standard is
    correctly configured and simply does not have it. A red cross next to
    "Assets" would send an administrator looking for a permission to grant
    that does not exist.
    """
    from app.integrations.jira.assets import (
        AssetsUnavailableError,
        discover_workspace,
    )
    try:
        discovered["assets_workspace"] = await discover_workspace(client)
        discovered["assets"] = "available"
    except AssetsUnavailableError:
        discovered["assets"] = "not on this site - needs JSM Premium or Enterprise"
    except JiraError as exc:
        discovered["assets"] = f"could not be determined: {exc}"


async def _check(checks: list[dict[str, Any]], name: str, coro: Any) -> Any:
    try:
        result = await coro
    except JiraError as exc:
        checks.append({"name": name, "ok": False, "detail": str(exc)})
        return None
    checks.append({"name": name, "ok": True, "detail": ""})
    return result


def parse_expiry(value: str | None) -> datetime | None:
    """When the operator says the credential lapses.

    Typed in rather than discovered, because Atlassian does not expose a
    token's expiry over the API. That makes it a promise rather than a fact -
    but a promise that produces a warning 30 days out is worth a great deal
    more than the silence that is the alternative.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise IntegrationError(
            "the expiry date must be ISO-8601, e.g. 2027-03-01") from None
    # UTC when the caller gave no zone, because the column is `timestamptz`
    # and a NAIVE value is interpreted in the database session's timezone.
    # On a server at +05:30 that turned a typed "2026-10-10" into
    # 2026-10-09T18:30Z - the expiry silently moved to the previous day, and
    # the alarm that warns 30 days out would have fired a day early for ever.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ------------------------------------------------------------- the inbound
#
# HOW A JIRA WEBHOOK ACTUALLY GETS REGISTERED, which is not what the obvious
# reading of the REST reference suggests.
#
# `POST /rest/api/3/webhook` - the "dynamic webhook" API - is restricted to
# Connect and OAuth 2.0 apps. A service account using Basic auth with an API
# token, which is what this product uses and what every self-hosted integration
# uses, gets a 403. So on Jira CLOUD the registration is done BY HAND in
# Settings > System > WebHooks, by an administrator, pasting a URL and a secret
# this platform generates.
#
# That is not a limitation worth hiding behind a spinner. The endpoint below
# provisions the URL and the secret, TRIES the automatic path where the
# deployment supports it, and otherwise returns exactly what to paste - which
# is a better experience than a "register" button that silently fails.
#
# Jira DATA CENTER is different: `/rest/webhooks/1.0/webhook` accepts a
# personal access token, so there the registration really is automatic.
#
# The difference matters for a second reason. A dynamic Cloud webhook EXPIRES
# after 30 days and must be refreshed; a hand-registered one does not expire at
# all. So `webhook_expires_at` is NULL for the manual path, and the refresh
# sweep only has work to do for registrations this platform made itself.

#: What we ask to be told about. Narrow on purpose: every event that is not
#: one of these is a delivery we would parse and discard, and Jira limits
#: concurrent deliveries per tenant.
WEBHOOK_EVENTS = ("jira:issue_updated", "jira:issue_deleted", "comment_created")

#: Only these two field changes matter. A description edit or a label added is
#: not something to re-acknowledge an alarm over.
WEBHOOK_FIELDS = ("status", "resolution")

DC_WEBHOOK = "/rest/webhooks/1.0/webhook"


async def webhook_secret(integration: dict[str, Any]) -> bytes | None:
    """The HMAC key for one integration's callback, decrypted.

    Takes the row rather than a session because the webhook endpoint has
    already looked the integration up by token - one query, before any body is
    parsed - and a second round trip on the hot path of a request that must
    answer inside 30 seconds would be for nothing.
    """
    blob = integration.get("webhook_blob")
    if not blob:
        return None
    payload = decrypt_secret(bytes(blob))
    secret = payload.get("secret")
    return secret.encode() if isinstance(secret, str) else None


def callback_url(token: str) -> str:
    """Where Jira posts. Absolute, because Jira is not on this network."""
    base = get_settings().public_base_url.rstrip("/")
    if not base:
        raise IntegrationError(
            "DCIM_PUBLIC_BASE_URL is not set, so there is no address to give "
            "Jira. A callback pointing at localhost would look configured and "
            "never deliver anything")
    return f"{base}/api/v1/integrations/jira/webhook/{token}"


def jql_filter(cfg: dict[str, Any]) -> str:
    """Only our own project, and only our own tickets inside it.

    Both halves matter. The project alone would deliver every human's ticket
    in a shared service desk; the label alone would deliver from projects this
    integration has nothing to do with.
    """
    label = cfg["labels"].get("prefix") or "dcim"
    project = cfg.get("project_key") or ""
    return (f'project = "{project}" AND labels = "{label}"' if project
            else f'labels = "{label}"')


async def provision_webhook(session: AsyncSession, integration: dict[str, Any]
                            ) -> dict[str, Any]:
    """Mint a callback token and secret, try to register, report what to do.

    Always provisions locally, even when the automatic registration is not
    available: the URL and the secret are what an administrator needs in order
    to do it by hand, and refusing to produce them because Jira said 403 would
    leave the operator with nothing.

    Rotating is the same operation. A new token and a new secret retire the
    old ones immediately, which is the correct behaviour for a credential
    somebody thinks may have leaked - the cost is that the Jira side has to be
    updated, which the response says.
    """
    token = new_webhook_token()
    secret = pysecrets.token_urlsafe(32)
    url = callback_url(token)

    registered: dict[str, Any] = {"automatic": False, "detail": ""}
    webhook_id = expires_at = None
    try:
        client = await build_client(session, integration)
    except JiraError as exc:
        registered["detail"] = str(exc)
        client = None

    if client is not None:
        try:
            webhook_id, expires_at = await _register_remote(
                client, integration, url, secret)
            registered["automatic"] = webhook_id is not None
        except JiraError as exc:
            # The expected outcome on Cloud with an API token, and not an
            # error: it is the documented restriction, not a misconfiguration.
            registered["detail"] = str(exc)
        finally:
            await client.aclose()

    await repo.set_webhook(session, integration["id"], token=token,
                           blob=encrypt_secret({"secret": secret}),
                           webhook_id=webhook_id, expires_at=expires_at)

    return {
        "url": url,
        # Returned ONCE, in the response to the request that created it, and
        # never readable again - the same contract as an API token. An
        # administrator has to paste it into Jira, so it cannot be write-only,
        # but it can be show-once.
        "secret": secret,
        "events": list(WEBHOOK_EVENTS),
        "jql": jql_filter(resolved(integration.get("config"))),
        "registered": registered,
        "instructions": _instructions(integration, registered),
    }


async def _register_remote(client: JiraClient, integration: dict[str, Any],
                           url: str, secret: str
                           ) -> tuple[str | None, datetime | None]:
    cfg = resolved(integration.get("config"))
    if integration["kind"] == "jira_dc":
        created = await client.post(DC_WEBHOOK, json_body={
            "name": f"DCIM - {integration['name']}",
            "url": url,
            "events": ["jira:issue_updated", "jira:issue_deleted",
                       "comment_created"],
            "filters": {"issue-related-events-section": jql_filter(cfg)},
            "excludeBody": False,
            "secret": secret,
        })
        # Data Center registrations do not expire, so there is nothing for the
        # refresh sweep to do and NULL says so rather than a far-future date
        # that would look like a deadline.
        return str((created or {}).get("self") or "dc"), None

    created = await client.post("/rest/api/3/webhook", json_body={
        "url": url,
        "webhooks": [{
            "events": list(WEBHOOK_EVENTS),
            "jqlFilter": jql_filter(cfg),
            "fieldIdsFilter": list(WEBHOOK_FIELDS),
        }],
    })
    results = (created or {}).get("webhookRegistrationResult") or []
    if not results or "createdWebhookId" not in (results[0] or {}):
        errors = "; ".join(str(e) for r in results
                           for e in (r or {}).get("errors") or [])
        raise JiraError(errors or "Jira accepted the call but registered nothing")
    # 30 days, which is what Jira gives a dynamic registration. Recorded as a
    # real deadline because it IS one: unrefreshed, this stops delivering a
    # month from now and nothing else would mention it.
    return (str(results[0]["createdWebhookId"]),
            datetime.now(UTC) + timedelta(days=30))


async def refresh_webhook(session: AsyncSession, integration: dict[str, Any]
                          ) -> datetime | None:
    """Push a dynamic registration's expiry out another 30 days.

    Only ever has work to do for a registration this platform made itself; a
    hand-registered webhook has no id here and never expires.
    """
    if not integration.get("webhook_id") or integration["kind"] == "jira_dc":
        return None
    client = await build_client(session, integration)
    try:
        result = await client.put("/rest/api/3/webhook/refresh", json_body={
            "webhookIds": [int(integration["webhook_id"])]})
    finally:
        await client.aclose()
    expires = _parse_expiry(result) or (datetime.now(UTC) + timedelta(days=30))
    await repo.touch_webhook_expiry(session, integration["id"], expires)
    return expires


def _parse_expiry(result: Any) -> datetime | None:
    raw = (result or {}).get("expirationDate") if isinstance(result, dict) else None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _instructions(integration: dict[str, Any],
                  registered: dict[str, Any]) -> str:
    if registered.get("automatic"):
        return ("Registered with Jira. Cloud registrations expire after 30 "
                "days and this platform refreshes them automatically; if the "
                "refresh ever fails you will see it on the alarm console.")
    where = ("Settings > System > WebHooks" if integration["kind"] == "jira_dc"
             else "Settings > System > WebHooks (site administration)")
    return (
        f"Automatic registration is not available for this credential "
        f"({registered.get('detail') or 'the API refused it'}). Jira Cloud "
        f"restricts the webhook API to Connect and OAuth apps, so an "
        f"administrator registers it by hand: in {where}, create a webhook "
        f"with the URL and secret above, tick Issue updated, Issue deleted "
        f"and Comment created, and paste the JQL filter. A hand-registered "
        f"webhook does not expire.")
