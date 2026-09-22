"""Configuring where tickets go, and seeing what happened to them.

Everything that writes is `admin`: an integration holds a credential to
somebody else's service desk, and the blast radius of a bad one is their
queue, not ours. Reading is `viewer`, because "is ticketing working" is a
question an operator asks at 03:00 and should not need an admin for.

No response from this router ever carries a credential. The repository's
column list is written so that `secret_enc` cannot arrive here by accident,
and what a caller gets instead is `secret_hint` - what KIND of secret exists
and how long it is, never what it is.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.config import get_settings
from app.core.security import (
    Principal,
    current_principal,
    encrypt_secret,
    require_role,
)
from app.db.session import get_session
from app.integrations import assets_sync
from app.integrations import fingerprint as fp
from app.integrations import policy as policy_mod
from app.integrations.config import DEFAULTS, resolved
from app.integrations.jira import assets_schema
from app.integrations.jira.assets import AssetsUnavailableError
from app.integrations.jira.client import JiraError
from app.repositories import alarms as alarm_repo
from app.repositories import integrations as repo
from app.services import integrations as service

router = APIRouter(prefix="/integrations", tags=["integrations"])


class SecretBody(BaseModel):
    model_config = {"extra": "forbid"}

    #: The Atlassian account email for a Cloud API token; unused for a PAT.
    username: str | None = Field(None, max_length=320)
    token: str = Field(min_length=1, max_length=4000)
    #: When the operator says it lapses. Atlassian does not expose a token's
    #: expiry over the API, so this is a promise rather than a fact - and a
    #: promise that warns 30 days out beats the silence that is the alternative.
    expires_at: str | None = None


class IntegrationCreate(BaseModel):
    model_config = {"extra": "forbid"}

    kind: str = Field(pattern="^(jira_cloud|jira_dc|jsm_ops)$")
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=1, max_length=500)
    cloud_id: str | None = Field(None, max_length=100)
    config: dict[str, Any] = Field(default_factory=dict)
    secret: SecretBody


class IntegrationPatch(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(None, min_length=1, max_length=100)
    base_url: str | None = Field(None, min_length=1, max_length=500)
    cloud_id: str | None = Field(None, max_length=100)
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    secret: SecretBody | None = None


class PolicyPreview(BaseModel):
    model_config = {"extra": "forbid"}

    policy: dict[str, Any] = Field(default_factory=dict)
    days: int = Field(7, ge=1, le=90)


# ------------------------------------------------------------------- read

@router.get("", summary="Configured ticketing integrations")
async def list_integrations(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    items = await repo.list_integrations(session)
    for item in items:
        item["effective"] = resolved(item.get("config"))
    return {"items": items, "defaults": DEFAULTS,
            # Surfaced rather than hidden: without it every ticket loses its
            # link back here, and the operator has no way to know that from
            # the settings page alone.
            "public_base_url": get_settings().public_base_url}


@router.get("/{integration_id}", summary="One integration")
async def get_integration(
    integration_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    row = await _must_exist(session, integration_id)
    row["effective"] = resolved(row.get("config"))
    return row


@router.get("/{integration_id}/outbox", summary="What is queued, and what failed")
async def outbox(
    integration_id: str,
    state: str | None = Query(None, pattern="^(pending|done|dead)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    await _must_exist(session, integration_id)
    return {"items": await repo.outbox_rows(session, integration_id,
                                            state=state, limit=limit,
                                            offset=offset)}


@router.get("/{integration_id}/links", summary="Conditions that have a ticket")
async def links(
    integration_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    row = await _must_exist(session, integration_id)
    items = await repo.recent_links(session, integration_id, limit=limit,
                                   offset=offset)
    for item in items:
        item["url"] = f"{row['base_url'].rstrip('/')}/browse/{item['issue_key']}"
    return {"items": items}


# ------------------------------------------------------------------ write

@router.post("", status_code=status.HTTP_201_CREATED,
             summary="Configure a ticketing integration")
async def create(
    body: IntegrationCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    try:
        base_url = service.check_base_url(body.kind, body.base_url)
        config = service.prepare_config(body.config)
        blob, hint, secret_kind = service.build_secret(
            body.kind, body.secret.model_dump())
        expires = service.parse_expiry(body.secret.expires_at)
    except service.IntegrationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    row = await repo.create_integration(
        session, kind=body.kind, name=body.name, base_url=base_url,
        cloud_id=body.cloud_id, config=config, blob=blob,
        secret_hint=hint, secret_kind=secret_kind, secret_expires_at=expires,
        actor=principal.username)
    await _audit(session, request, principal, "integration.create", row["id"],
                 after={"name": body.name, "kind": body.kind,
                        "base_url": base_url})
    await session.commit()
    return row


@router.patch("/{integration_id}", summary="Change an integration")
async def patch(
    integration_id: str,
    body: IntegrationPatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    before = await _must_exist(session, integration_id)

    blob = hint = secret_kind = None
    expires = None
    config = None
    base_url = None
    try:
        if body.base_url is not None:
            base_url = service.check_base_url(before["kind"], body.base_url)
        if body.config is not None:
            config = service.prepare_config(body.config)
        if body.secret is not None:
            blob, hint, secret_kind = service.build_secret(
                before["kind"], body.secret.model_dump())
            expires = service.parse_expiry(body.secret.expires_at)
    except service.IntegrationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if body.enabled:
        # Checked against what the row will BE, not what it was: a request
        # that sets the project key and enables it in one call must be judged
        # on the new key.
        candidate = dict(before)
        if config is not None:
            candidate["config"] = config
        reason = service.ready_to_enable(candidate)
        if reason:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"this integration cannot be enabled yet: {reason}")

    row = await repo.update_integration(
        session, integration_id, actor=principal.username, name=body.name,
        base_url=base_url, cloud_id=body.cloud_id, enabled=body.enabled,
        config=config, blob=blob, secret_hint=hint,
        secret_kind=secret_kind, secret_expires_at=expires)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "integration not found")

    await _audit(session, request, principal, "integration.update",
                 integration_id,
                 before={"enabled": before["enabled"],
                         "config": before.get("config")},
                 after={"enabled": row["enabled"], "config": row.get("config"),
                        "credential_rotated": blob is not None})
    await session.commit()
    return row


@router.delete("/{integration_id}", summary="Remove an integration")
async def delete(
    integration_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    before = await _must_exist(session, integration_id)
    # The links go with it, by CASCADE. Worth knowing before pressing it:
    # every open ticket loses its association with the condition that opened
    # it, so a later recurrence opens a second one.
    if not await repo.delete_integration(session, integration_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "integration not found")
    await _audit(session, request, principal, "integration.delete",
                 integration_id, before={"name": before["name"],
                                         "kind": before["kind"]})
    await session.commit()
    return {"ok": True, "integration_id": integration_id}


@router.post("/{integration_id}/test", summary="Prove the connection works")
async def test(
    integration_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    row = await _must_exist(session, integration_id)
    # The handler never holds a credential: the service loads and decrypts it,
    # which is the rule `test_secret_access` enforces - in a handler the
    # plaintext is one `return` away from the wire.
    return await service.test_connection(session, row)


@router.post("/{integration_id}/policy/preview",
             summary="What this policy would have ticketed")
async def preview(
    integration_id: str,
    body: PolicyPreview,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    """Replay a candidate policy over the alarms actually raised recently.

    The affordance that makes a policy editable by somebody who is not willing
    to experiment on a live service desk. `POST /maintenance/windows/preview`
    exists for the same reason and this deliberately mirrors it.
    """
    row = await _must_exist(session, integration_id)
    merged = dict(resolved(row.get("config"))["policy"])
    merged.update(body.policy or {})

    alarms = await repo.recent_alarms_for_preview(session, days=body.days)
    matched, reasons = [], {}
    # Distinct CONDITIONS per facet, not rows: the same fault raised four times
    # is one ticket, and a facet strip counting rows would tell an operator a
    # category is four times as expensive as it is.
    by_category: dict[str, set[str]] = {}
    by_severity: dict[str, set[str]] = {}
    for alarm in alarms:
        why = policy_mod.explain(alarm, merged)
        if why:
            reasons[why] = reasons.get(why, 0) + 1
            continue
        print_ = fp.of_alarm(alarm)
        by_category.setdefault(alarm.get("category") or "—", set()).add(print_)
        by_severity.setdefault(alarm["severity"], set()).add(print_)
        matched.append({
            "alarm_id": alarm["id"], "device_name": alarm.get("device_name"),
            "severity": alarm["severity"], "alarm_type": alarm["alarm_type"],
            "first_seen": alarm["first_seen"],
            "fingerprint": fp.of_alarm(alarm),
        })

    # Per DAY as well as a total, because the number that decides whether a
    # policy is sane is "how many tickets a day", and a 7-day total divided in
    # somebody's head is where the decision goes wrong.
    per_day: dict[str, int] = {}
    for item in matched:
        day = str(item["first_seen"])[:10]
        per_day[day] = per_day.get(day, 0) + 1

    # Distinct fingerprints, not rows: the same condition raised four times is
    # ONE ticket, and reporting four would make every policy look four times
    # as expensive as it is.
    tickets = len({m["fingerprint"] for m in matched})
    return {"considered": len(alarms), "matched": len(matched),
            "tickets": tickets, "per_day": per_day,
            "by_category": {k: len(v) for k, v in by_category.items()},
            "by_severity": {k: len(v) for k, v in by_severity.items()},
            "excluded": sorted(reasons.items(), key=lambda kv: -kv[1]),
            "sample": matched[:25]}


@router.post("/{integration_id}/outbox/{row_id}/retry",
             summary="Put a dead letter back in the queue")
async def retry(
    integration_id: str,
    row_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    await _must_exist(session, integration_id)
    if not await repo.revive(session, integration_id, row_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "no dead message with that id")
    await _audit(session, request, principal, "integration.retry",
                 integration_id, after={"outbox_row": row_id})
    await session.commit()
    return {"ok": True, "row": row_id}



# --------------------------------------------------------------- inbound

@router.get("/{integration_id}/inbox", summary="What Jira has told us")
async def inbox(
    integration_id: str,
    state: str | None = Query(None, pattern="^(pending|done|dead)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    await _must_exist(session, integration_id)
    return {"items": await repo.inbound_rows(session, integration_id,
                                             state=state, limit=limit,
                                             offset=offset)}


@router.post("/{integration_id}/webhooks/register",
             summary="Mint a callback URL and secret for Jira")
async def register_webhook(
    integration_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Provision the inbound half, and say how to finish it.

    The secret is in the response and nowhere else, ever - the same contract
    as an Atlassian API token. It has to be readable once because an
    administrator pastes it into Jira by hand on Cloud, where the webhook API
    is restricted to Connect and OAuth apps and a Basic-auth service account
    gets a 403.

    Calling this again ROTATES both the token and the secret, which retires
    the old ones immediately. That is the right behaviour for a credential
    somebody thinks has leaked, and the response says the Jira side now has
    to be updated.
    """
    row = await _must_exist(session, integration_id)
    try:
        result = await service.provision_webhook(session, row)
    except service.IntegrationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await _audit(session, request, principal, "integration.webhook.register",
                 integration_id,
                 after={"automatic": result["registered"]["automatic"],
                        "url": result["url"]})
    await session.commit()
    return result


# ------------------------------------------------------------ assets export

class AssetsConfig(BaseModel):
    model_config = {"extra": "forbid"}

    #: The object schema id in Jira. Created by an administrator, not by this
    #: platform: an integration that could create schemas could also replace
    #: one somebody else's team depends on.
    schema_id: str | None = Field(None, max_length=40)
    #: The import source's id, for the operator's own reference.
    import_id: str | None = Field(None, max_length=80)
    #: The import configuration's token. Write-only, like every other secret
    #: here - stored encrypted and never readable again.
    import_token: str | None = Field(None, max_length=4000)


@router.get("/{integration_id}/assets", summary="The CMDB export's state")
async def assets_status(
    integration_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    await _must_exist(session, integration_id)
    state = await repo.assets_state(session, integration_id)
    # What this platform WOULD send, so an operator can see the shape before
    # committing to it - and so the object types they have to create in Jira
    # are listed rather than described in documentation nobody opens.
    state["schema"] = [
        {"type": entry["label"],
         "attributes": [label for label, _f, _k in entry["attributes"]]}
        for entry in assets_schema.SCHEMA
    ]
    state["one_way"] = True
    return state


@router.put("/{integration_id}/assets", summary="Configure the CMDB export")
async def configure_assets(
    integration_id: str,
    body: AssetsConfig,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    await _must_exist(session, integration_id)
    blob = None
    if body.import_token:
        blob = encrypt_secret({"token": body.import_token.strip()})
    await repo.set_assets_config(
        session, integration_id, schema_id=body.schema_id,
        import_id=body.import_id, import_token_blob=blob)
    await _audit(session, request, principal, "integration.assets.configure",
                 integration_id,
                 after={"schema_id": body.schema_id,
                        "import_id": body.import_id,
                        "token_rotated": blob is not None})
    await session.commit()
    return await repo.assets_state(session, integration_id)


@router.post("/{integration_id}/assets/sync", summary="Push inventory to Assets")
async def sync_assets(
    integration_id: str,
    request: Request,
    full: bool = False,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Export the estate into JSM Assets.

    ONE WAY, always: the DCIM is the system of record for physical
    infrastructure, and a CMDB that could write back into rack elevations
    would be a data corruption vector wearing a synchronisation label.

    `full` re-sends every device rather than those changed since the last run.
    An explicit button rather than a default, because a full push of a large
    estate will meet the Assets external-import rate limiter.
    """
    row = await _must_exist(session, integration_id)
    if not row["enabled"]:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "this integration is disabled")
    await _audit(session, request, principal, "integration.assets.sync",
                 integration_id, after={"full": full})
    await session.commit()

    from app.db.session import unit_of_work
    try:
        result = await assets_sync.run(unit_of_work, row, full=full)
    except assets_sync.AssetsNotConfiguredError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except AssetsUnavailableError as exc:
        # A licence fact, not a failure. Reported as its own status so the UI
        # can say "this needs Premium" rather than "the connection failed".
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc)) from exc
    except JiraError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return result

# ------------------------------------------------- the manual escape hatch

@router.post("/alarms/{alarm_id}/ticket", summary="Open a ticket for one alarm")
async def ticket_alarm(
    alarm_id: str,
    request: Request,
    integration_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    """Ticket a condition the policy did not match.

    This is what lets the default policy stay conservative. An operator who
    wants a ticket for a MINOR gets one in a click, so the policy never has to
    be widened to cover a judgement call - and widening a policy to cover one
    case is how a service desk ends up with four hundred tickets a day.
    """
    wide = await repo.alarms_for_export(session, [alarm_id])
    alarm = wide.get(alarm_id)
    if alarm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alarm not found")

    integrations = await repo.active(session)
    if integration_id:
        integrations = [i for i in integrations if i["id"] == integration_id]
    if not integrations:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "no enabled ticketing integration")
    target = integrations[0]

    print_ = fp.of_alarm(alarm)
    existing = await repo.links_for(session, target["id"], [print_])
    if print_ in existing and existing[print_].get("closed_at") is None:
        return {"ok": True, "already": True,
                "issue_key": existing[print_]["issue_key"]}

    await repo.enqueue(session, [{
        "integration_id": target["id"], "kind": "alarm_raised",
        "fingerprint": print_, "alarm_id": alarm_id, "payload": alarm}])
    if alarm.get("device_id"):
        # A platform alarm has no device, and `alarm_history.device_id` is NOT
        # NULL - the audit row below carries the fact either way.
        await alarm_repo.record_history(
            session, alarm_id=alarm_id, device_id=alarm["device_id"],
            action="ticket_requested", severity=alarm["severity"],
            actor=principal.username, detail={"integration": target["name"]})
    await _audit(session, request, principal, "integration.ticket",
                 target["id"], after={"alarm_id": alarm_id,
                                      "fingerprint": print_})
    await session.commit()
    # Queued, not created: the HTTP call happens on the dispatcher, so the
    # honest answer here is "it is on its way", not an issue key we do not
    # have yet.
    return {"ok": True, "queued": True, "fingerprint": print_,
            "integration": target["name"]}


@router.get("/alarms/{alarm_id}/ticket", summary="The ticket for one alarm")
async def alarm_ticket(
    alarm_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    link = await repo.link_for_alarm(session, alarm_id)
    if link is None:
        return {"linked": False}
    link["url"] = f"{link['base_url'].rstrip('/')}/browse/{link['issue_key']}"
    link["linked"] = True
    return link


# ----------------------------------------------------------------- pieces

async def _must_exist(session: AsyncSession, integration_id: str
                      ) -> dict[str, Any]:
    row = await repo.get_integration(session, integration_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "integration not found")
    return row


async def _audit(session: AsyncSession, request: Request, principal: Principal,
                 action: str, target_id: str, *, before: Any = None,
                 after: Any = None) -> None:
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action=action,
                       target_type="integration", target_id=target_id,
                       ip=ip, user_agent=agent, before=before, after=after)
