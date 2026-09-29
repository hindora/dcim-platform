"""Collectors: what each is running, and what it has been told to run.

Separate from /collector, which is the collector's own API and speaks a
collector token. This is the operator's view of the same thing.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.logging import get_logger
from app.core.security import (
    Principal,
    current_principal,
    forget_collector,
    mint_collector_token,
    require_role,
)
from app.db.session import get_session
from app.repositories import collector as fleet_repo
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
    from app.services import collector as fleet

    plan = await fleet.ownership(session)
    owned: dict[str, int] = {}
    for owner in plan.values():
        if owner:
            owned[owner] = owned.get(owner, 0) + 1
    rows = await repo.list_collectors(session)
    for r in rows:
        # What the PLAN gives it, beside what its heartbeat says it polls. The
        # two differ for a collector that is stale, pending or mid-move - which
        # are the cases this page is opened for.
        r["planned"] = owned.get(r["id"], 0)
    sites = (await session.execute(text("""
        SELECT id::text, code, name FROM datacenter ORDER BY code
    """))).mappings().all()
    return {
        "collectors": rows,
        "sites": [dict(s) for s in sites],
        # Endpoints no collector can own: a site with nobody placed in it.
        "unassigned": sum(1 for o in plan.values() if o is None),
        # The schema travels with the data so the form is not a second copy of
        # the rules, drifting from the one the server validates against.
        "schema": cfg.describe(),
    }


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class CreateBody(BaseModel):
    model_config = {"extra": "forbid"}

    #: Letters, digits, '-' and '_'. No dot: the token format uses it.
    id: str = Field(..., min_length=1, max_length=64)
    datacenter_id: str | None = None


class PatchBody(BaseModel):
    """Placement and intent. Absent means unchanged; a null site means "any"."""

    model_config = {"extra": "forbid"}

    datacenter_id: str | None = None
    state: Literal["active", "draining"] | None = None


async def _must_exist(session: AsyncSession, collector_id: str) -> dict[str, Any]:
    row = await fleet_repo.collector_state(session, collector_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such collector")
    return row


async def _check_site(session: AsyncSession, datacenter_id: str | None) -> None:
    if datacenter_id is None:
        return
    known = (await session.execute(text("""
        SELECT 1 FROM datacenter WHERE id::text = :id
    """), {"id": datacenter_id})).scalar()
    if not known:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no such site")


@router.post("", status_code=status.HTTP_201_CREATED,
             summary="Create a collector ahead of its install")
async def create_collector(
    body: CreateBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Create, place and issue a token in one step - the order that is safe.

    A collector that simply starts up and asks for work arrives pending once
    another exists. Creating it here first means it is placed in its site and
    approved before it runs, so its first assignment is the right one. The
    token is returned exactly once; the platform keeps no copy it could show
    again, only the generation that makes it valid.
    """
    if not _ID.match(body.id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "a collector id is letters, digits, '-' and '_'")
    await _check_site(session, body.datacenter_id)
    if not await fleet_repo.create_collector(session, body.id, body.datacenter_id,
                                             principal.username):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"a collector called {body.id} already exists")
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="collector.create", target_type="collector",
                       target_id=body.id, ip=ip, user_agent=agent,
                       after={"datacenter_id": body.datacenter_id})
    await session.commit()
    forget_collector(body.id)
    log.info("collector created", collector_id=body.id,
             actor=principal.username, datacenter_id=body.datacenter_id)
    return {"id": body.id, "token": mint_collector_token(body.id, generation=1),
            "generation": 1}


@router.patch("/{collector_id}", summary="Place a collector, approve it, or drain it")
async def patch_collector(
    collector_id: str,
    body: PatchBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Placement moves endpoints between collectors, so it is admin-only.

    Approving a pending collector is setting it active. Draining takes it out
    of the hash - its unpinned endpoints move to the others in its site - and
    setting it active again brings it back.
    """
    before = await _must_exist(session, collector_id)
    if before["state"] == "decommissioned":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "a decommissioned collector cannot be changed")
    changes = body.model_dump(exclude_unset=True)
    after: dict[str, Any] = {}
    if ("datacenter_id" in changes
            and changes["datacenter_id"] != before.get("datacenter_id")):
        await _check_site(session, changes["datacenter_id"])
        await fleet_repo.set_placement(session, collector_id,
                                       changes["datacenter_id"])
        after["datacenter_id"] = changes["datacenter_id"]
    if changes.get("state") and changes["state"] != before["state"]:
        await fleet_repo.set_state(session, collector_id, changes["state"],
                                   principal.username)
        after["state"] = changes["state"]
    if not after:
        return {"id": collector_id, "changed": {}}
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="collector.update", target_type="collector",
                       target_id=collector_id, ip=ip, user_agent=agent,
                       before={k: before.get(k) for k in after}, after=after)
    await session.commit()
    forget_collector(collector_id)
    log.info("collector updated", collector_id=collector_id,
             actor=principal.username, **after)
    return {"id": collector_id, "changed": after}


@router.post("/{collector_id}/token", summary="Issue a new token, revoking the old")
async def issue_token(
    collector_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """The only way a collector token leaves this platform, and only once.

    Issuing is revoking: the generation moves on, so the token the collector
    holds now stops working within the revocation cache's fifteen seconds. Put
    the new one in its environment and restart it.
    """
    row = await _must_exist(session, collector_id)
    if row["state"] == "decommissioned":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "a decommissioned collector cannot be issued a token")
    generation = await fleet_repo.bump_token_generation(session, collector_id)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="collector.token_issued", target_type="collector",
                       target_id=collector_id, ip=ip, user_agent=agent,
                       after={"generation": generation})
    await session.commit()
    forget_collector(collector_id)
    log.info("collector token issued", collector_id=collector_id,
             actor=principal.username, generation=generation)
    return {"id": collector_id, "generation": generation,
            "token": mint_collector_token(collector_id, generation=generation)}


@router.post("/{collector_id}/decommission",
             summary="Retire a collector and refuse its token")
async def decommission(
    collector_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Replaces deleting the collector_instance row, which lost the history.

    The row stays, marked retired; its token stops working; anything pinned to
    it goes back to the hash so the rest of its site picks it up. Not
    reversible from here on purpose - a machine coming back from retirement is
    a new install and should be created as one.
    """
    row = await _must_exist(session, collector_id)
    if row["state"] == "decommissioned":
        return {"id": collector_id, "unpinned": 0}
    unpinned = await fleet_repo.unpin_all(session, collector_id)
    await fleet_repo.set_state(session, collector_id, "decommissioned",
                               principal.username)
    await fleet_repo.bump_token_generation(session, collector_id)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="collector.decommission", target_type="collector",
                       target_id=collector_id, ip=ip, user_agent=agent,
                       before={"state": row["state"]},
                       after={"state": "decommissioned", "unpinned": unpinned})
    await session.commit()
    forget_collector(collector_id)
    log.warning("collector decommissioned", collector_id=collector_id,
                actor=principal.username, unpinned=unpinned)
    return {"id": collector_id, "unpinned": unpinned}


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
