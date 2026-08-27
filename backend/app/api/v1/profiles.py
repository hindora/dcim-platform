"""Poll profiles: how often, how patiently, and what is asked for.

Separate from devices because a profile is not one device's property - it is
the schedule several hundred endpoints follow at once, which is also why every
route here is admin-only and every edit says how many endpoints it moved.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.logging import get_logger
from app.core.mappings_gen import MAPPING_GROUPS
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.services import devices as service
from app.services import poll_profile_config

router = APIRouter(prefix="/poll-profiles", tags=["poll profiles"])
log = get_logger("api.poll_profiles")


class PollProfileBody(BaseModel):
    """A poll profile, whole or in part.

    `name` is accepted only on create. The importer selects profiles by name,
    so a rename would send the next import somewhere else without failing.
    """

    model_config = {"extra": "forbid"}

    name: str | None = None
    interval_s: int | None = Field(None, ge=0, le=86_400)
    timeout_ms: int | None = Field(None, ge=250, le=120_000)
    retries: int | None = Field(None, ge=0, le=5)
    metric_groups: list[str] | None = None
    push_enabled: bool | None = None


@router.get("", summary="Poll profiles and what they steer")
async def poll_profiles(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """`metric_groups` comes back with the list because a profile that names a
    group no mapping file defines collects nothing at all, silently - the UI
    has to offer the real ones rather than a text box.
    """
    return {
        "profiles": await service.poll_profiles(session),
        "metric_groups": {k: list(v) for k, v in MAPPING_GROUPS.items()},
        "limits": {
            "min_interval_s": poll_profile_config.MIN_INTERVAL_S,
            "max_interval_s": poll_profile_config.MAX_INTERVAL_S,
            "min_timeout_ms": poll_profile_config.MIN_TIMEOUT_MS,
            "max_timeout_ms": poll_profile_config.MAX_TIMEOUT_MS,
            "max_retries": poll_profile_config.MAX_RETRIES,
        },
    }


@router.get("/{profile_id}/usage",
            summary="What an edit to this profile would move")
async def poll_profile_usage(
    profile_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    rows = await service.poll_profile_usage(session, profile_id)
    return {"profile_id": profile_id, "breakdown": rows,
            "endpoints": sum(r["endpoints"] for r in rows),
            "devices": sum(r["devices"] for r in rows)}


@router.post("", status_code=status.HTTP_201_CREATED,
             summary="Create a poll profile")
async def create_poll_profile(
    body: PollProfileBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Admin, not operator.

    Moving one endpoint between profiles is an operator's call; creating the
    thing hundreds of endpoints will follow is a change to how the estate is
    polled.
    """
    try:
        created = await service.create_poll_profile(
            session, body.model_dump(exclude_unset=True))
    except poll_profile_config.PollProfileError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            str(exc)) from None
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="poll_profile.create", target_type="poll_profile",
                       target_id=created["id"], ip=ip, user_agent=agent,
                       after=created)
    await session.commit()
    log.info("poll profile created", name=created["name"],
             actor=principal.username)
    return created


@router.patch("/{profile_id}", summary="Edit a poll profile")
async def update_poll_profile(
    profile_id: str,
    body: PollProfileBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """One edit here moves every endpoint that follows the profile.

    The response says how many, and so does the audit row: "interval 60 -> 20"
    means nothing six months later without "on 310 endpoints" beside it.
    """
    try:
        before, after, followers = await service.update_poll_profile(
            session, profile_id, body.model_dump(exclude_unset=True))
    except service.PollProfileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "no such poll profile") from None
    except poll_profile_config.PollProfileError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            str(exc)) from None

    if after:
        ip, agent = audit.client_of(request)
        await audit.record(
            session, actor=audit.actor_of(principal),
            action="poll_profile.update", target_type="poll_profile",
            target_id=profile_id, ip=ip, user_agent=agent,
            before=before, after={**after, "endpoints_moved": followers})
        await session.commit()
        log.info("poll profile updated", profile_id=profile_id,
                 actor=principal.username, changed=sorted(after),
                 endpoints_moved=followers)
    return {"profile_id": profile_id, "changed": after,
            "endpoints_moved": followers}
