"""Collector pools: the operator's view of site x plane placement.

docs/26 Phase 5 created `collector_pool` (migration 0088) and the assigner
has honoured it ever since - but until this router nothing could create
one, so every deployment ran with zero pools and every collector placed by
`datacenter_id` alone. This is the missing write path, plus the derived
per-pool firewall matrix that phase's deploy row asked for.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import pools as repo
from app.services import pools as service
from app.services import shard_map

router = APIRouter(prefix="/pools", tags=["pools"])
log = get_logger("api.pools")


class CreateBody(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1, max_length=64)
    datacenter_id: str
    plane: Literal["it_oob", "bms", "production", "other"]
    cidrs: list[str] = Field(default_factory=list)
    trap_vip: str | None = None
    bbmd_settings: dict[str, Any] = Field(default_factory=dict)
    rate_budget_points_per_s: int | None = None
    min_members: int = Field(1, ge=1)
    #: Per protocol: {"max_concurrent"?, "min_interval_ms"?} at each address.
    target_limits: dict[str, Any] = Field(default_factory=dict)


class PatchBody(BaseModel):
    """Absent means unchanged. Site and plane are the pool's identity and
    are deliberately not here - see services.pools.clean."""

    model_config = {"extra": "forbid"}

    name: str | None = Field(None, min_length=1, max_length=64)
    cidrs: list[str] | None = None
    trap_vip: str | None = None
    bbmd_settings: dict[str, Any] | None = None
    rate_budget_points_per_s: int | None = None
    min_members: int | None = Field(None, ge=1)
    target_limits: dict[str, Any] | None = None


def _http(exc: service.PoolError) -> HTTPException:
    if isinstance(exc, service.PoolNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, "no such pool")
    if isinstance(exc, service.PoolConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


@router.get("", summary="Every pool, with members, ownership and what is unassigned")
async def list_pools(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    out = await service.overview(session)
    # The create form needs the sites to offer, the same list /collectors
    # serves - fetched here so the page is one request, not two.
    sites = (await session.execute(text("""
        SELECT id::text, code, name FROM datacenter ORDER BY code
    """))).mappings().all()
    out["sites"] = [dict(s) for s in sites]
    return out


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a pool")
async def create_pool(
    body: CreateBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Admin only: a pool changes which collectors may own which endpoints
    the moment a collector is placed in it, the same reason placing a
    collector is admin only."""
    try:
        pool = await service.create(session, body.model_dump())
    except service.PoolError as exc:
        raise _http(exc) from None
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="pool.create", target_type="collector_pool",
                       target_id=pool["id"], ip=ip, user_agent=agent,
                       after={k: pool[k] for k in ("name", "datacenter_id", "plane",
                                                    "cidrs", "trap_vip", "bbmd_settings",
                                                    "rate_budget_points_per_s",
                                                    "min_members", "target_limits")})
    await session.commit()
    log.info("pool created", pool_id=pool["id"], name=pool["name"],
             site=pool["site"], plane=pool["plane"], actor=principal.username)
    return pool


@router.get("/{pool_id}", summary="One pool: members, endpoints, ranges")
async def get_pool(
    pool_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    try:
        return await service.detail(session, pool_id)
    except service.PoolError as exc:
        raise _http(exc) from None


@router.patch("/{pool_id}", summary="Change a pool's settings")
async def patch_pool(
    pool_id: str,
    body: PatchBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    try:
        before, after = await service.update(session, pool_id,
                                             body.model_dump(exclude_unset=True))
    except service.PoolError as exc:
        raise _http(exc) from None
    if not after:
        return {"id": pool_id, "changed": {}}
    ip, agent = audit.client_of(request)
    # A BBMD or trap VIP change re-routes traffic for every member on the
    # next assignment fetch, so the trail keeps the before as well.
    await audit.record(session, actor=audit.actor_of(principal),
                       action="pool.update", target_type="collector_pool",
                       target_id=pool_id, ip=ip, user_agent=agent,
                       before=before, after=after)
    await session.commit()
    log.info("pool updated", pool_id=pool_id, actor=principal.username,
             changed=sorted(after))
    return {"id": pool_id, "changed": after}


@router.delete("/{pool_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete an empty pool")
async def delete_pool(
    pool_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> Response:
    """Refused while any collector is placed in it - move them first. An
    endpoint's explicit pool override is cleared by the FK; a range-resolved
    endpoint simply stops resolving to a pool and falls back to unplaced
    collectors, which is the pre-pools behaviour, not a loss."""
    before = await repo.get_pool(session, pool_id)
    try:
        await service.delete(session, pool_id)
    except service.PoolError as exc:
        raise _http(exc) from None
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="pool.delete", target_type="collector_pool",
                       target_id=pool_id, ip=ip, user_agent=agent,
                       before={k: (before or {}).get(k) for k in ("name", "site", "plane")})
    await session.commit()
    log.warning("pool deleted", pool_id=pool_id, actor=principal.username)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{pool_id}/firewall-matrix",
            summary="The firewall rules this pool needs, derived from its ranges")
async def firewall_matrix(
    pool_id: str,
    format: Literal["json", "text"] = "json",
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
    settings: Settings = Depends(get_settings),
) -> Any:
    """`?format=text` returns the same rows as a plain-text change request
    (text/plain), ready to paste into a network team's ticket."""
    try:
        pool = await service.detail(session, pool_id)
    except service.PoolError as exc:
        raise _http(exc) from None
    matrix = service.firewall_matrix(pool, pool["ranges"], pool["protocols"],
                                     settings.public_base_url)
    if format == "text":
        return Response(content=matrix["text"], media_type="text/plain")
    return matrix


@router.get("/{pool_id}/readiness",
            summary="Is this pool collecting yet, and which bring-up step is not done")
async def readiness(
    pool_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """The onboarding wizard's credentials and verification steps (docs/26
    Phase 8): per-protocol credential coverage and comm state for the
    endpoints resolving here, each member's latest preflight, and a list of
    checks that are True, False, or None when there is nothing to judge."""
    try:
        return await service.readiness(session, pool_id)
    except service.PoolError as exc:
        raise _http(exc) from None


@router.get("/{pool_id}/rebalance-preview",
            summary="What rebalancing this pool now would move, and where")
async def rebalance_preview(
    pool_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """docs/26 Phase 5's "rebalance now (with preview)". The assigner damps
    ordinary rebalances - a member must sit 10 endpoints AND 2x off the
    pool mean - so a member added to a large, balanced-enough pool can sit
    under-used. This computes the pass the assigner would make with this
    pool's damping bypassed: pins, HA quarantine and change freezes still
    apply, and no other pool is touched."""
    if await repo.get_pool(session, pool_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such pool")
    return await shard_map.rebalance_preview(session, pool_id)


@router.post("/{pool_id}/rebalance", summary="Rebalance this pool now, bypassing damping")
async def rebalance_now(
    pool_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Runs the real assigner with this pool forced. Refused during a change
    freeze covering the pool: a freeze exists so nothing moves, and an
    operator who must move work then has drain and pins, both explicit.
    Retries briefly if an ingest worker's tick holds the assigner lock."""
    import asyncio

    from app.services import assigner

    pool = await repo.get_pool(session, pool_id)
    if pool is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such pool")
    preview = await shard_map.rebalance_preview(session, pool_id)
    if preview["frozen"]:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "a change freeze covers this pool's site - nothing is moved "
                            "automatically until it ends")
    result = None
    for _ in range(10):
        result = await assigner.run(session, force_pool=pool_id)
        if result.ran:
            break
        await asyncio.sleep(0.5)
    if result is None or not result.ran:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "the assigner is busy on another worker - retry in a moment")
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="pool.rebalance",
                       target_type="collector_pool", target_id=pool_id, ip=ip,
                       user_agent=agent, before=preview["before"],
                       after={"after": preview["after"], "moving": preview["moving"],
                              "recorded_moves": result.moved})
    await session.commit()
    log.info("pool rebalanced", pool_id=pool_id, actor=principal.username,
             moving=preview["moving"], recorded=result.moved)
    return {"pool_id": pool_id, "preview": preview, "recorded_moves": result.moved}
