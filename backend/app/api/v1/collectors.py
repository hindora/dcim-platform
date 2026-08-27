"""Collectors: what each is running, and what it has been told to run.

Separate from /collector, which is the collector's own API and speaks a
collector token. This is the operator's view of the same thing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import collector_config as repo
from app.services import collector_config as cfg

router = APIRouter(prefix="/collectors", tags=["collectors"])
log = get_logger("api.collectors")


class ConfigBody(BaseModel):
    """The complete document the page is showing.

    Whole-document rather than a patch, because "clear this override and fall
    back to the collector's file" has to be expressible, and in a patch the
    absence of a key already means "leave it alone".
    """

    model_config = {"extra": "forbid"}

    config: dict[str, Any]


@router.get("", summary="Collectors, with stored and running configuration")
async def list_collectors(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {
        "collectors": await repo.list_collectors(session),
        # The schema travels with the data so the form is not a second copy of
        # the rules, drifting from the one the server validates against.
        "schema": cfg.describe(),
    }


@router.put("/{collector_id}/config", summary="Set what a collector runs")
async def set_config(
    collector_id: str,
    body: ConfigBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Admin only, and never the collector's own identity or credentials.

    Those stay in the file on the host: break the path to the control plane
    from the control plane and nobody can repair it from the control plane
    either.
    """
    before = await repo.get(session, collector_id)
    try:
        clean = cfg.validate(body.config)
    except cfg.CollectorConfigError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            str(exc)) from None

    pending = cfg.restart_needed(before["config"], clean)
    saved = await repo.put(session, collector_id, clean, principal.username)

    ip, agent = audit.client_of(request)
    # A listener move can silence a whole plane without erroring anywhere, so
    # the trail records the before as well as the after.
    await audit.record(session, actor=audit.actor_of(principal),
                       action="collector_config.set", target_type="collector",
                       target_id=collector_id, ip=ip, user_agent=agent,
                       before={"version": before["version"],
                               "config": before["config"]},
                       after={"version": saved["version"], "config": clean,
                              "restart_pending": pending})
    await session.commit()
    log.info("collector config saved", collector_id=collector_id,
             actor=principal.username, version=saved["version"],
             restart_pending=len(pending))
    return {"collector_id": collector_id, "version": saved["version"],
            "config": saved["config"],
            # Named fields rather than a count: "3 settings need a restart" is
            # not something an operator can act on without knowing which.
            "restart_pending": pending}
