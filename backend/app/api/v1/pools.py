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
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import pools as repo
from app.services import pools as service

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
    return await service.overview(session)


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
                                                    "min_members")})
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
