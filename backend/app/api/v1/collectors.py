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
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import (
    Principal,
    current_principal,
    forget_collector,
    forget_collector_cert,
    mint_collector_token,
    require_role,
)
from app.db.session import get_session
from app.repositories import collector as fleet_repo
from app.repositories import collector_config as repo
from app.repositories import preflight as preflight_repo
from app.services import collector_config as cfg
from app.services import collector_pki

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
    #: docs/26 Phase 5: set, the pool is the WHOLE placement (site and
    #: plane) and datacenter_id follows the pool's own site.
    pool_id: str | None = None


class PatchBody(BaseModel):
    """Placement and intent. Absent means unchanged; a null site means "any"."""

    model_config = {"extra": "forbid"}

    datacenter_id: str | None = None
    #: Null takes the collector out of its pool - back to site-only
    #: placement, which is what it had before pools existed.
    pool_id: str | None = None
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


async def _check_pool(session: AsyncSession, pool_id: str | None) -> None:
    if pool_id is None:
        return
    known = (await session.execute(text("""
        SELECT 1 FROM collector_pool WHERE id::text = :id
    """), {"id": pool_id})).scalar()
    if not known:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no such pool")


@router.post("", status_code=status.HTTP_201_CREATED,
             summary="Create a collector ahead of its install")
async def create_collector(
    body: CreateBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Create, place, and issue enrollment material in one step - the order
    that is safe.

    A collector that simply starts up and asks for work arrives pending once
    another exists. Creating it here first means it is placed in its site and
    approved before it runs, so its first assignment is the right one.

    Two credentials come back, both exactly once - the platform keeps no copy
    of either. The enrollment token is what the install command actually
    uses: `dcim-collector enroll` exchanges it for a certificate and never
    touches the bearer token at all. The bearer token is minted anyway, at
    generation 1, purely as a fallback for a site that cannot yet run the
    mTLS proxy in front of this platform - see migration 0086's docstring for
    why that path stays open rather than being cut off in this release.
    """
    if not _ID.match(body.id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "a collector id is letters, digits, '-' and '_'")
    await _check_site(session, body.datacenter_id)
    await _check_pool(session, body.pool_id)
    if not await fleet_repo.create_collector(session, body.id, body.datacenter_id,
                                             principal.username, pool_id=body.pool_id):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"a collector called {body.id} already exists")
    if body.pool_id:
        # The pool's own site wins over any datacenter_id sent alongside it.
        await fleet_repo.set_pool(session, body.id, body.pool_id)
    token, expires_at = await collector_pki.issue_enrollment_token(
        session, body.id, principal.username)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="collector.create", target_type="collector",
                       target_id=body.id, ip=ip, user_agent=agent,
                       after={"datacenter_id": body.datacenter_id,
                              "pool_id": body.pool_id,
                              "enrollment_token_expires_at": expires_at.isoformat()})
    await session.commit()
    forget_collector(body.id)
    log.info("collector created", collector_id=body.id,
             actor=principal.username, datacenter_id=body.datacenter_id,
             pool_id=body.pool_id)
    server = settings.public_base_url or "https://<this platform's address>"
    return {
        "id": body.id,
        "enrollment": {
            "token": token,
            "expires_at": expires_at,
            "install_command": (
                f"dcim-collector enroll --server {server} "
                f"--id {body.id} --token {token}"),
        },
        "fallback_bearer_token": mint_collector_token(body.id, generation=1),
        "generation": 1,
    }


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
    if "pool_id" in changes and changes["pool_id"] != before.get("pool_id"):
        await _check_pool(session, changes["pool_id"])
        await fleet_repo.set_pool(session, collector_id, changes["pool_id"])
        after["pool_id"] = changes["pool_id"]
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


@router.post("/{collector_id}/enrollment-token",
             summary="Issue a fresh one-time enrollment token")
async def issue_enrollment_token(
    collector_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """For a collector whose token expired unused, or that needs to re-enroll
    after its certificate was revoked. Does not touch any certificate the
    collector already holds and is currently using - only `/revoke-cert`
    does that - so reissuing a token for an already-enrolled collector is
    harmless until someone actually uses the new token."""
    row = await _must_exist(session, collector_id)
    if row["state"] == "decommissioned":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "a decommissioned collector cannot be enrolled")
    token, expires_at = await collector_pki.issue_enrollment_token(
        session, collector_id, principal.username)
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="collector.enrollment_token_issued",
                       target_type="collector", target_id=collector_id,
                       ip=ip, user_agent=agent,
                       after={"expires_at": expires_at.isoformat()})
    await session.commit()
    log.info("enrollment token issued", collector_id=collector_id,
             actor=principal.username)
    server = settings.public_base_url or "https://<this platform's address>"
    return {"id": collector_id, "token": token, "expires_at": expires_at,
            "install_command": (
                f"dcim-collector enroll --server {server} "
                f"--id {collector_id} --token {token}")}


@router.post("/{collector_id}/revoke-cert",
             summary="Stop trusting this collector's current certificate")
async def revoke_cert(
    collector_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """For a suspected key compromise: unlike `/decommission`, the collector
    row and its history stay - it is expected to re-enroll with a fresh
    token (`/enrollment-token`) and resume where it left off, not be
    replaced. Its bearer token, if it still has one, is untouched; if this
    platform's proxy is the only path in for it, revoking the certificate
    alone is enough to lock it out."""
    await _must_exist(session, collector_id)
    before_status = await collector_pki.cert_status(session, collector_id)
    if not await collector_pki.revoke_cert(session, collector_id):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"collector {collector_id} has no certificate to revoke, "
                            f"or it is already revoked")
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="collector.cert_revoked", target_type="collector",
                       target_id=collector_id, ip=ip, user_agent=agent,
                       before={"cert_serial": (before_status or {}).get("cert_serial")})
    await session.commit()
    forget_collector_cert(collector_id)
    log.warning("collector certificate revoked", collector_id=collector_id,
                actor=principal.username)
    return {"id": collector_id, "revoked": True}


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


@router.get("/{collector_id}/preflight",
            summary="Preflight history for one collector (docs/26 Phase 8)")
async def preflight_history(
    collector_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Read-only for any authenticated user, like the rest of this file's
    GET routes - only placement and config changes need admin. Newest
    first; `latest` is `history[0]` when there is one at all, broken out
    separately so a caller that only cares about "did it pass" does not
    have to reach into an array."""
    rows = await preflight_repo.history(session, collector_id)
    return {"collector_id": collector_id,
           "latest": rows[0] if rows else None,
           "history": rows}
