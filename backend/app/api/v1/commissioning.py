"""The commissioning queue: devices whose wire has moved ahead of their record.

Read-only on purpose. Confirming a row is an ordinary lifecycle transition, so
the button posts to `/devices/{id}/lifecycle` and gets the matrix check, the
event and the audit row that every other transition gets. A second write path
here would be a second set of rules to keep in step.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import commissioning as repo
from app.services import sim_import as sync_service

log = get_logger("api.commissioning")

router = APIRouter(prefix="/commissioning", tags=["commissioning"])


@router.get("/queue", summary="Devices racked, built or answering when they should not be")
async def queue(
    soak_hours: int = Query(repo.DEFAULT_SOAK_HOURS, ge=0, le=720, description=(
        "Hours a machine must have been `installed` before acceptance is "
        "offered. A burn-in is 24-48h; lower it to see everything.")),
    limit: int = Query(500, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    items = await repo.ready_queue(session, soak_hours=soak_hours, limit=limit)
    return {
        "soak_hours": soak_hours,
        "counts": await repo.counts(session, soak_hours=soak_hours),
        "items": items,
    }


# ------------------------------------------------------------------- the sync
#
# The DCIM cannot see a device somebody racked until it reads the device plane
# again, and that read was a shell command - which makes the whole commissioning
# path unusable by the operator it was built for. These two endpoints are the
# button and the thing it polls.


@router.get("/sync", summary="What a sync is doing, and what the last ones did")
async def sync_status(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return await sync_service.status(session)


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED,
             summary="Read the estate from the device plane")
async def sync_start(
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    """Accepted, not created: the import takes about ninety seconds.

    Audited like any other privileged action, because it is one - a sync adds
    devices, decommissions the ones that have gone, and rewrites placement and
    endpoints across the estate.
    """
    actor = audit.actor_of(principal)
    try:
        run = await sync_service.start(session, actor=actor)
    except sync_service.AlreadyRunningError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except sync_service.NotConfiguredError as exc:
        # 503 rather than 500: the code is fine, the deployment is not, and the
        # message names the setting to fill in.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None

    ip, agent = audit.client_of(request)
    await audit.record(session, actor=actor, action="inventory.sync",
                       target_type="sim_import_run", target_id=run["id"],
                       ip=ip, user_agent=agent, after={"source": run.get("source")})
    await session.commit()
    log.info("sync queued", run_id=run["id"], actor=actor)
    return run
