"""Alarm, event and rule endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
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
    include_symptoms: bool = Query(
        False, description="Symptoms are hidden by default: one root cause with "
                           "twenty symptoms should read as one incident."),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    states = state or ["ACTIVE", "ACKNOWLEDGED"]
    items = await repo.list_alarms(
        session, states=states, severities=severity, device_id=device_id,
        alarm_type=alarm_type, include_symptoms=include_symptoms, limit=limit)
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


@router.post("/alarm-rules/{rule_id}/enable", summary="Enable a rule")
async def enable_rule(
    rule_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    if not await repo.set_rule_enabled(session, rule_id, True):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    await session.commit()
    return {"ok": True, "rule_id": rule_id, "enabled": True}


@router.post("/alarm-rules/{rule_id}/disable", summary="Disable a rule")
async def disable_rule(
    rule_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    if not await repo.set_rule_enabled(session, rule_id, False):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    await session.commit()
    return {"ok": True, "rule_id": rule_id, "enabled": False}
