"""Device actions: switch a PDU outlet, restart a server through its BMC.

Routing and validation only; the rules live in services/device_actions.py.
The write itself is made by the collector that owns the device's endpoint, so
these routes queue a command and report how it went.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import actions as repo
from app.services import device_actions as svc

router = APIRouter(prefix="/devices", tags=["device-actions"])
log = get_logger("api.device_actions")


def _fail(exc: svc.ActionError) -> HTTPException:
    return HTTPException(exc.code, str(exc))


@router.get("/{device_id}/actions", summary="What can be done to this device from here")
async def get_actions(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    cap = await svc.capabilities(session, device_id, principal.role)
    if cap is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
    cap["recent"] = [{
        "id": r["id"], "state": r["state"], "by": r["created_by"], "at": r["created_at"],
        "finished_at": r["finished_at"],
        "label": (r["payload"] or {}).get("label"),
        "detail": (r["result"] or {}).get("detail"),
    } for r in await repo.recent_actions(session, device_id)]
    return cap


class ActionBody(BaseModel):
    model_config = {"extra": "forbid"}
    action: str
    outlet: int | None = Field(None, ge=1, le=999)
    #: The device's name, typed by the operator, for the actions that need it.
    confirm: str | None = None
    reason: str | None = Field(None, max_length=300)


@router.post("/{device_id}/actions/preview", summary="What an action would take down")
async def preview_action(
    device_id: str, body: ActionBody,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    try:
        return await svc.preview(session, device_id, body.action, body.outlet, principal.role)
    except svc.ActionError as exc:
        raise _fail(exc) from None


@router.post("/{device_id}/actions", status_code=status.HTTP_202_ACCEPTED,
             summary="Do something to this device")
async def run_action(
    device_id: str, body: ActionBody, request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    """Queue the action for the collector that owns the device's endpoint.

    Returns at once with the command id; the write happens on the collector
    within a second or two, and GET .../actions/{id} says how it went. Every
    request, refused or not, that gets as far as the queue leaves an audit row
    naming who asked, what, and why."""
    try:
        queued = await svc.submit(
            session, device_id=device_id, action_id=body.action, outlet=body.outlet,
            confirm=body.confirm, reason=body.reason, actor=principal.username,
            role=principal.role)
    except svc.ActionError as exc:
        await session.rollback()
        raise _fail(exc) from None
    ip, agent = audit.client_of(request)
    p = queued["payload"]
    await audit.record(session, actor=audit.actor_of(principal), action="device.action",
                       target_type="device", target_id=device_id, ip=ip, user_agent=agent,
                       before=None,
                       after={"action": body.action, "outlet": body.outlet,
                              "reason": p.get("reason"), "label": p.get("label"),
                              "command_id": queued["command_id"],
                              "collector": queued["collector"]})
    await session.commit()
    log.info("device action queued", device_id=device_id, action=body.action,
             outlet=body.outlet, actor=principal.username, collector=queued["collector"],
             command_id=queued["command_id"])
    return {"command_id": queued["command_id"], "state": "pending",
            "collector": queued["collector"], "label": p.get("label")}


@router.get("/{device_id}/actions/{command_id}", summary="How an action went")
async def action_status(
    device_id: str, command_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    row = await repo.action_status(session, device_id, command_id)
    await session.commit()                       # the expiry sweep above is a write
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such action on this device")
    return {"id": row["id"], "state": row["state"], "by": row["created_by"],
            "label": (row["payload"] or {}).get("label"),
            "detail": (row["result"] or {}).get("detail"),
            "created_at": row["created_at"], "finished_at": row["finished_at"]}
