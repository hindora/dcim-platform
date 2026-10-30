"""Alarm, event and rule endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.alarms import correlation, link_correlation, rule_edit
from app.alarms.service import bump_rules_version
from app.core import audit
from app.core.alert_taxonomy import CATEGORIES, DETECTIONS, RESPONSE_CLASSES
from app.core.config import get_settings
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import alarms as repo

router = APIRouter(tags=["alarms"])


class AckRequest(BaseModel):
    note: str | None = Field(None, max_length=1000)


@router.get("/alarms", summary="List alarms (roots only by default)")
async def list_alarms(
    state: list[str] | None = Query(None,
        description="ACTIVE, ACKNOWLEDGED, CLEARED. Defaults to the open ones."),
    severity: list[str] | None = Query(None),
    device_id: str | None = None,
    alarm_type: str | None = None,
    category: list[str] | None = Query(None,
        description="Domain of the failing thing: " + ", ".join(CATEGORIES)),
    detection: list[str] | None = Query(None,
        description="How it was found: " + ", ".join(DETECTIONS)
                    + ". An attribute, not a category - filtering on `derived` "
                      "shows what analysis noticed across every domain."),
    response_class: list[str] | None = Query(None,
        description="Required response: " + ", ".join(RESPONSE_CLASSES)
                    + ". `alarm` demands action now and expects an "
                      "acknowledgement; `alert` is informational and belongs to "
                      "whoever schedules the work."),
    room: str | None = Query(None,
        description="Only conditions in this room, resolved through the rack "
                    "or off the device - the same location the roll-up uses."),
    rack: str | None = Query(None,
        description="Only conditions on devices standing in this rack."),
    include_symptoms: bool = Query(
        False, description="Symptoms are hidden by default: one root cause with "
                           "twenty symptoms should read as one incident."),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, description=(
        "Rows to skip. The order is total, so walking the list in pages of "
        "`limit` from offset 0 sees every row once.")),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    for value, allowed, what in ((category, CATEGORIES, "category"),
                                 (detection, DETECTIONS, "detection"),
                                 (response_class, RESPONSE_CLASSES,
                                  "response_class")):
        unknown = sorted(set(value or []) - set(allowed))
        if unknown:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"unknown {what}: {', '.join(unknown)}")
    states = state or ["ACTIVE", "ACKNOWLEDGED"]
    items = await repo.list_alarms(
        session, states=states, severities=severity, device_id=device_id,
        alarm_type=alarm_type, categories=category, detections=detection,
        response_classes=response_class, room_id=room, rack_id=rack,
        include_symptoms=include_symptoms, limit=limit, offset=offset)
    return {"items": items}


@router.get("/alarms/summary", summary="Counts by severity and state")
async def alarm_summary(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return await repo.summary(session)


@router.get("/alarms/{alarm_id}", summary="One alarm")
async def get_alarm(
    alarm_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    alarm = await repo.get_alarm(session, alarm_id)
    if alarm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alarm not found")
    return alarm


@router.post("/alarms/{alarm_id}/acknowledge", summary="Acknowledge an alarm")
async def acknowledge(
    alarm_id: str,
    body: AckRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    row = await repo.acknowledge(session, alarm_id, principal.username, body.note)
    if row is None:
        # Already acknowledged or cleared. Not an error - two operators reaching
        # for the same alarm is normal - but the caller should know nothing moved.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "alarm is not in the ACTIVE state")
    await repo.record_history(session, alarm_id=row["id"], device_id=row["device_id"],
                              action="acknowledged", severity=row["severity"],
                              actor=principal.username,
                              detail={"note": body.note} if body.note else None)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="alarm.acknowledge", target_type="alarm",
                       target_id=alarm_id, ip=ip, user_agent=agent,
                       before={"state": "ACTIVE", "severity": row["severity"]},
                       after={"state": "ACKNOWLEDGED", "note": body.note})
    await session.commit()
    return {"ok": True, "alarm_id": alarm_id, "acknowledged_by": principal.username}


@router.post("/alarms/{alarm_id}/clear", summary="Clear an alarm by hand")
async def clear(
    alarm_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    row = await repo.manual_clear(session, alarm_id, principal.username)
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "alarm is already cleared")
    await repo.record_history(session, alarm_id=row["id"], device_id=row["device_id"],
                              action="cleared", severity=row["severity"],
                              actor=principal.username)
    # Clearing a ROOT by hand has to let go of what it was explaining.
    #
    # Suppression is only ever a display decision, and it is safe because the
    # root is the row an operator acts on: when the root goes, the symptoms
    # come back and stand on their own. Every automatic clear path did this.
    # This one - the button an operator actually presses - did not, so a
    # hand-cleared root left its symptoms flagged as explained by an alarm that
    # no longer existed: invisible on the console, and unclearable, because the
    # thing that would have released them was already gone.
    #
    # Found on a live board: a temperature critical cleared by hand at 09:20
    # left the warning under it suppressed with a CLEARED root, still at 93 C.
    for sym in await correlation.release_symptoms(session, row["id"]):
        await repo.record_history(
            session, alarm_id=sym["id"], device_id=sym["device_id"],
            action="released", severity=sym["severity"],
            actor=principal.username, detail={"root": row["id"]})
    await link_correlation.refresh_link_state(
        session, device_id=row["device_id"], instance=row["instance"] or "")
    await repo.refresh_device_alarm_state(session, [row["device_id"]])
    ip, agent = audit.client_of(request)
    # A manual clear is the one alarm action that can hide a live fault, so it
    # is the one most worth attributing.
    await audit.record(session, actor=audit.actor_of(principal),
                       action="alarm.clear", target_type="alarm",
                       target_id=alarm_id, ip=ip, user_agent=agent,
                       before={"severity": row["severity"],
                               "device_id": row["device_id"]},
                       after={"state": "CLEARED"})
    await session.commit()
    return {"ok": True, "alarm_id": alarm_id, "cleared_by": principal.username}


@router.get("/events", summary="Recent events")
async def list_events(
    device_id: str | None = None,
    event_type: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {"items": await repo.list_events(
        session, device_id=device_id, event_type=event_type, limit=limit)}


@router.get("/events/unresolved-sources",
            summary="Traps whose source matched no known endpoint")
async def unresolved_events(
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    # Kept as a first-class view: a trap from an address inventory does not know
    # about means the network and the CMDB disagree, which is worth seeing
    # rather than silently discarding.
    return {"items": await repo.list_events(session, unresolved_only=True, limit=limit)}


@router.get("/alarm-rules", summary="List alarm rules")
async def list_rules(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {"items": await repo.list_rules(session)}


async def _bump() -> None:
    """Tell every worker the rules changed, so the edit lands on the next
    batch rather than after the refresh interval."""
    redis = Redis.from_url(get_settings().redis_url)
    try:
        await bump_rules_version(redis)
    finally:
        await redis.aclose()


async def _toggle(rule_id: str, enabled: bool, request: Request,
                  session: AsyncSession, principal: Principal) -> dict[str, Any]:
    before = await repo.get_rule(session, rule_id)
    if before is None or not await repo.set_rule_enabled(session, rule_id, enabled):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="alarm_rule.enable" if enabled else "alarm_rule.disable",
                       target_type="alarm_rule", target_id=rule_id, ip=ip, user_agent=agent,
                       before={"name": before["name"], "enabled": before["enabled"]},
                       after={"enabled": enabled})
    await session.commit()
    await _bump()
    return {"ok": True, "rule_id": rule_id, "enabled": enabled}


@router.post("/alarm-rules/{rule_id}/enable", summary="Enable a rule")
async def enable_rule(
    rule_id: str, request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    return await _toggle(rule_id, True, request, session, principal)


@router.post("/alarm-rules/{rule_id}/disable", summary="Disable a rule")
async def disable_rule(
    rule_id: str, request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    return await _toggle(rule_id, False, request, session, principal)


class RulePatch(BaseModel):
    model_config = {"extra": "forbid"}
    threshold: float | None = None
    clear_threshold: float | None = None
    dwell_samples: int | None = None
    dwell_seconds: int | None = None
    clear_dwell_samples: int | None = None
    severity: str | None = None
    message_tpl: str | None = None


def _json_safe(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (v if isinstance(v, (int, float, str, bool)) or v is None else str(v))
            for k, v in d.items()}


@router.patch("/alarm-rules/{rule_id}",
              summary="Edit a rule's threshold, dwell, severity or message")
async def edit_rule(
    rule_id: str, body: RulePatch, request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Change estate policy for a rule. Admin only: this moves the alarm for
    every device of the type. A device that needs its own value gets an
    override instead."""
    rule = await repo.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    try:
        raw = body.model_dump(exclude_unset=True)
        changes = rule_edit.validate_fields(rule, raw, rule_edit.RULE_FIELDS)
        changes = rule_edit.resolve_band(rule, changes)
    except rule_edit.RuleEditError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    changes = {k: v for k, v in changes.items() if rule.get(k) != v}
    if not changes:
        return {"ok": True, "rule_id": rule_id, "changed": []}
    await repo.update_rule(session, rule_id, changes, principal.username)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="alarm_rule.edit",
                       target_type="alarm_rule", target_id=rule_id, ip=ip, user_agent=agent,
                       before=_json_safe({"name": rule["name"]}
                                         | {k: rule.get(k) for k in changes}),
                       after=_json_safe(changes))
    await session.commit()
    await _bump()
    return {"ok": True, "rule_id": rule_id, "changed": sorted(changes),
            "rule": await repo.get_rule(session, rule_id)}


class RuleNew(BaseModel):
    model_config = {"extra": "forbid"}
    name: str
    metric_key: str
    operator: str
    threshold: float
    clear_threshold: float | None = None
    severity: str
    device_types: list[str] = Field(default_factory=list)
    dwell_samples: int = 3
    dwell_seconds: int | None = None
    clear_dwell_samples: int = 2
    message_tpl: str | None = None


@router.post("/alarm-rules", status_code=status.HTTP_201_CREATED,
             summary="Write a new threshold rule")
async def create_rule(
    body: RuleNew, request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    types = {r[0] for r in (await session.execute(text(
        "SELECT code FROM device_type"))).all()}
    try:
        fields = rule_edit.validate_new(body.model_dump(), types)
    except rule_edit.RuleEditError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    try:
        rule_id = await repo.insert_rule(session, fields, principal.username)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "a rule with that name already exists") from None
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="alarm_rule.create",
                       target_type="alarm_rule", target_id=rule_id, ip=ip, user_agent=agent,
                       before=None,
                       after=_json_safe(
                           fields | {"device_types": ",".join(fields["device_types"])}))
    await session.commit()
    await _bump()
    return {"ok": True, "rule_id": rule_id, "rule": await repo.get_rule(session, rule_id)}


@router.delete("/alarm-rules/{rule_id}", summary="Delete a rule you wrote")
async def delete_rule(
    rule_id: str, request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    rule = await repo.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    if not rule["custom"]:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "only a rule you wrote can be deleted; turn a built-in one off instead")
    await repo.delete_custom_rule(session, rule_id)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="alarm_rule.delete",
                       target_type="alarm_rule", target_id=rule_id, ip=ip, user_agent=agent,
                       before=_json_safe({"name": rule["name"]}), after=None)
    await session.commit()
    await _bump()
    return {"ok": True, "rule_id": rule_id}


# ------------------------------------------------- per-device overrides

@router.get("/devices/{device_id}/alarm-rules",
            summary="The rules that apply to a device, with its own exceptions")
async def device_rules(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    rows = await repo.device_rules(session, device_id)
    if rows is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
    return {"items": rows}


class OverrideBody(BaseModel):
    model_config = {"extra": "forbid"}
    threshold: float | None = None
    clear_threshold: float | None = None
    dwell_samples: int | None = None
    dwell_seconds: int | None = None
    clear_dwell_samples: int | None = None
    severity: str | None = None
    #: false mutes the rule on this device; null follows the rule
    enabled: bool | None = None
    reason: str | None = Field(None, max_length=300)


@router.put("/devices/{device_id}/alarm-rules/{rule_id}",
            summary="Give one device its own value for a rule, or mute it there")
async def put_override(
    device_id: str, rule_id: str, body: OverrideBody, request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    """A per-device exception. Operators may set one: it is scoped to a single
    device, reversible, and audited with the before and after. A mute needs a
    reason, because a muted alarm is one nobody will see."""
    rule = await repo.get_rule(session, rule_id)
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    try:
        fields = rule_edit.validate_fields(
            rule, body.model_dump(exclude_unset=True), rule_edit.OVERRIDE_FIELDS)
        if fields.get("enabled") is False and not fields.get("reason"):
            raise rule_edit.RuleEditError("say why the rule is muted on this device")
        before = await repo.get_override(session, rule_id, device_id)
        # An override moves the raise point and its clear point together or not
        # at all, judged against the rule it overrides.
        eff = rule | {k: v for k, v in (before or {}).items() if v is not None}
        fields = rule_edit.resolve_band(rule, fields, inherited=eff)
        merged = {**(before or {}), **fields}
        if not any(merged.get(k) is not None for k in rule_edit.OVERRIDE_FIELDS if k != "reason"):
            raise rule_edit.RuleEditError("an override that changes nothing is not an override")
    except rule_edit.RuleEditError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    try:
        await repo.upsert_override(session, rule_id, device_id, merged, principal.username)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found") from None
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="alarm_rule.override",
                       target_type="device", target_id=device_id, ip=ip, user_agent=agent,
                       before=_json_safe({"rule": rule["name"], **(before or {})}),
                       after=_json_safe({"rule": rule["name"], **merged}))
    await session.commit()
    await _bump()
    return {"ok": True, "device_id": device_id, "rule_id": rule_id}


@router.delete("/devices/{device_id}/alarm-rules/{rule_id}",
               summary="Remove a device's exception: it follows the rule again")
async def remove_override(
    device_id: str, rule_id: str, request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    before = await repo.get_override(session, rule_id, device_id)
    if before is None or not await repo.delete_override(session, rule_id, device_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "that device has no override on this rule")
    rule = await repo.get_rule(session, rule_id)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="alarm_rule.override_remove", target_type="device",
                       target_id=device_id, ip=ip, user_agent=agent,
                       before=_json_safe({"rule": (rule or {}).get("name"), **before}),
                       after=None)
    await session.commit()
    await _bump()
    return {"ok": True, "device_id": device_id, "rule_id": rule_id}
