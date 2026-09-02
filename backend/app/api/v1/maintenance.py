"""Maintenance windows, records, and the preview that stops a window being
scoped too widely at 02:00."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import maintenance as repo
from app.services import maintenance as service

router = APIRouter(prefix="/maintenance", tags=["maintenance"])
log = get_logger("api.maintenance")


class WindowCreate(BaseModel):
    model_config = {"extra": "forbid"}

    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    change_ref: str | None = Field(None, max_length=100)
    kind: str = Field("planned", pattern="^(planned|emergency)$")
    starts_at: datetime
    ends_at: datetime
    #: False schedules the work without silencing anything - a window somebody
    #: wants on the calendar but not in the alarm path.
    suppress: bool = True
    device_ids: list[str] = Field(default_factory=list)


class TargetsBody(BaseModel):
    model_config = {"extra": "forbid"}
    device_ids: list[str]


class PreviewBody(BaseModel):
    model_config = {"extra": "forbid"}
    device_ids: list[str]


class RecordCreate(BaseModel):
    model_config = {"extra": "forbid"}

    kind: str = Field(pattern="^(preventive|corrective|firmware|replacement)$")
    summary: str = Field(min_length=1, max_length=500)
    detail: str | None = None
    window_id: str | None = None
    parts_used: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/windows", summary="Maintenance windows")
async def list_windows(
    status_filter: str | None = Query(None, alias="status"),
    device_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {"items": await repo.list_windows(
        session, status=status_filter, device_id=device_id, limit=limit)}


@router.post("/windows/preview", summary="What this window would cover")
async def preview_window(
    body: PreviewBody,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Declared BEFORE /windows/{id}: FastAPI matches in declaration order, and
    a static path after a parameterised one is never reached - "preview" would
    be looked up as a window id.

    Everything returned comes from the impact graph and the power chain that
    already exist, so this is a screen rather than a second implementation of
    reachability.
    """
    return await service.preview(session, body.device_ids)


@router.post("/windows", status_code=status.HTTP_201_CREATED,
             summary="Schedule a maintenance window")
async def create_window(
    body: WindowCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    if body.ends_at <= body.starts_at:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "a window must end after it starts")
    actor = audit.actor_of(principal)
    window_id = await repo.create_window(
        session, title=body.title, description=body.description,
        change_ref=body.change_ref, kind=body.kind, starts_at=body.starts_at,
        ends_at=body.ends_at, suppress=body.suppress, created_by=actor)
    if body.device_ids:
        await repo.set_targets(session, window_id, body.device_ids)

    ip, agent = audit.client_of(request)
    # A window silences alarms on real equipment. Who scheduled it, over what,
    # and for how long has to be attributable afterwards.
    await audit.record(session, actor=actor, action="maintenance.window.create",
                       target_type="maintenance_window", target_id=window_id,
                       ip=ip, user_agent=agent, before=None,
                       after={"title": body.title, "kind": body.kind,
                              "starts_at": body.starts_at.isoformat(),
                              "ends_at": body.ends_at.isoformat(),
                              "suppress": body.suppress,
                              "targets": len(body.device_ids)})
    await session.commit()
    return await repo.get_window(session, window_id)


@router.get("/windows/{window_id}", summary="One window, its targets and what it shelved")
async def get_window(
    window_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    window = await repo.get_window(session, window_id)
    if window is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such window")
    window["targets"] = await repo.targets(session, window_id)
    # Listed, not merely counted. "Did anything ELSE break while we were in
    # there" is the question asked after every window, and this is the only
    # place it can be answered.
    window["shelved"] = await repo.shelved_alarms(session, window_id)
    return window


@router.post("/windows/{window_id}/targets", summary="Add devices to a window")
async def add_targets(
    window_id: str,
    body: TargetsBody,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    if await repo.get_window(session, window_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such window")
    added = await repo.set_targets(session, window_id, body.device_ids)
    await session.commit()
    return {"added": added}


@router.delete("/windows/{window_id}/targets/{device_id}",
               summary="Remove a device from a window")
async def remove_target(
    window_id: str,
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    await repo.remove_target(session, window_id, device_id)
    await session.commit()
    return {"status": "removed"}


async def _advance(session: AsyncSession, principal: Principal, request: Request,
                   window_id: str, action: str) -> dict[str, Any]:
    window = await repo.get_window(session, window_id)
    if window is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such window")

    legal = {"start": ("scheduled",),
             "complete": ("active",),
             "cancel": ("scheduled", "active")}[action]
    if window["status"] not in legal:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"a {window['status']} window cannot be {action}ed; "
            f"expected one of {', '.join(legal)}")

    if action == "start":
        shelved = await service.activate(session, window_id)
        result = {"status": "active", "shelved_alarms": shelved}
    else:
        new_status = "completed" if action == "complete" else "cancelled"
        devices = await service.complete(session, window_id, new_status)
        result = {"status": new_status, "unshelved_devices": len(devices)}

    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action=f"maintenance.window.{action}",
                       target_type="maintenance_window", target_id=window_id,
                       ip=ip, user_agent=agent,
                       before={"status": window["status"]}, after=result)
    await session.commit()
    return result


@router.post("/windows/{window_id}/start", summary="Start a window early")
async def start_window(
    window_id: str, request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    return await _advance(session, principal, request, window_id, "start")


@router.post("/windows/{window_id}/complete", summary="End a window")
async def complete_window(
    window_id: str, request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    return await _advance(session, principal, request, window_id, "complete")


@router.post("/windows/{window_id}/cancel", summary="Cancel a window")
async def cancel_window(
    window_id: str, request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    return await _advance(session, principal, request, window_id, "cancel")
