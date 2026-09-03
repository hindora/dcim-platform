"""Consumable parts, stores, stock movements and capacity reservations."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import parts as repo
from app.repositories import reservations as res_repo

router = APIRouter(tags=["inventory"])


# ------------------------------------------------------------------- parts

class PartIn(BaseModel):
    model_config = {"extra": "forbid"}

    sku: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(pattern="^(psu|fan|memory|disk|optic|cable|controller"
                                  "|battery|filter|other)$")
    vendor_id: str | None = None
    fits_types: list[str] = Field(default_factory=list)
    unit_cost: float | None = None
    currency: str | None = Field(None, min_length=3, max_length=3)


class MovementIn(BaseModel):
    """One movement. There is deliberately no endpoint that SETS a quantity."""

    model_config = {"extra": "forbid"}

    store_id: str
    delta: int = Field(description="positive receipt, negative consumption")
    reason: str = Field(pattern="^(receipt|consumed|adjustment|rma|transfer)$")
    device_id: str | None = None
    record_id: str | None = None
    note: str | None = None


class ReorderIn(BaseModel):
    model_config = {"extra": "forbid"}
    store_id: str
    reorder_at: int | None = Field(None, ge=0)
    reorder_to: int | None = Field(None, ge=0)


class StoreIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=120)
    datacenter_id: str | None = None
    room_id: str | None = None
    location_note: str | None = None


@router.get("/parts", summary="Consumable parts, short stock first")
async def list_parts(
    category: str | None = None,
    below_reorder: bool = False,
    search: str | None = Query(None, max_length=120),
    limit: int = Query(200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {"items": await repo.list_parts(
        session, category=category, below_reorder=below_reorder,
        search=search, limit=limit)}


@router.post("/parts", status_code=status.HTTP_201_CREATED)
async def create_part(
    body: PartIn,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    try:
        part_id = await repo.create_part(session, **body.model_dump())
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "error": "sku_exists",
            "message": f"a part with SKU {body.sku} already exists",
        }) from exc
    return {"id": part_id}


@router.get("/parts/{part_id}")
async def get_part(
    part_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    part = await repo.get_part(session, part_id)
    if part is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such part")
    part["stock"] = await repo.stock_of(session, part_id)
    return part


@router.delete("/parts/{part_id}")
async def delete_part(
    part_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    # A part with a ledger is not deletable: removing it takes the record of
    # what was fitted where along with it.
    if await repo.has_history(session, part_id):
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "error": "has_history",
            "message": "this part has stock movements; it cannot be deleted "
                       "without losing the record of what was fitted where",
        })
    await repo.delete_part(session, part_id)
    await session.commit()
    return {"status": "deleted"}


@router.get("/parts/{part_id}/movements", summary="The ledger for one part")
async def list_movements(
    part_id: str,
    limit: int = Query(200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {"items": await repo.movements(session, part_id, limit)}


@router.post("/parts/{part_id}/movements", status_code=status.HTTP_201_CREATED,
             summary="The only way stock changes")
async def post_movement(
    part_id: str,
    body: MovementIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    """Post a movement. There is no endpoint that sets `on_hand` directly.

    Correcting a miscount is an `adjustment` carrying a note, which the schema
    requires - the same operation, under a name that leaves a record.
    """
    actor = audit.actor_of(principal)
    try:
        movement_id = await repo.move(
            session, part_id=part_id, store_id=body.store_id, delta=body.delta,
            reason=body.reason, actor=actor, device_id=body.device_id,
            record_id=body.record_id, note=body.note)
    except repo.InsufficientStockError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "error": "insufficient_stock",
            "message": str(exc),
            "wanted": exc.wanted,
            "have": exc.have,
        }) from None
    except Exception as exc:
        await session.rollback()
        if "ck_stock_movement_adjustment_note" in str(exc):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {
                "error": "adjustment_needs_note",
                "message": "an adjustment overrides the ledger with a physical "
                           "count, so it has to say why",
            }) from None
        raise

    ip, agent = audit.client_of(request)
    await audit.record(session, actor=actor, action="stock.movement",
                       target_type="part", target_id=part_id, ip=ip,
                       user_agent=agent, before=None,
                       after={"delta": body.delta, "reason": body.reason,
                              "store_id": body.store_id})
    await session.commit()
    return {"id": movement_id}


@router.post("/parts/{part_id}/reorder", summary="Set the reorder point")
async def set_reorder(
    part_id: str,
    body: ReorderIn,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    await repo.set_reorder(session, part_id=part_id, store_id=body.store_id,
                           reorder_at=body.reorder_at, reorder_to=body.reorder_to)
    await session.commit()
    return {"status": "set"}


@router.get("/stores")
async def list_stores(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {"items": await repo.list_stores(session)}


@router.post("/stores", status_code=status.HTTP_201_CREATED)
async def create_store(
    body: StoreIn,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    store_id = await repo.create_store(session, **body.model_dump())
    await session.commit()
    return {"id": store_id}


@router.get("/stock/reconcile",
            summary="Where the running total and the ledger disagree")
async def reconcile(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Should always be empty.

    `move` writes the balance and the movement in one transaction, so a
    non-empty result means something wrote `part_stock` from outside that path -
    which is the failure the ledger design exists to prevent.
    """
    rows = await repo.reconcile(session)
    return {"discrepancies": rows, "ok": not rows}


# ------------------------------------------------------------ reservations

class ReservationIn(BaseModel):
    model_config = {"extra": "forbid"}

    project: str = Field(min_length=1, max_length=120)
    owner_group: str | None = None
    rack_id: str | None = None
    room_id: str | None = None
    u_start: int | None = Field(None, ge=1)
    u_height: int | None = Field(None, ge=1)
    power_kw: float | None = None
    cool_kw: float | None = None
    needed_by: date | None = None
    #: Required. A reservation with no end date is how a rack stays held for a
    #: project cancelled two years ago.
    expires_at: date
    notes: str | None = None


class FulfilIn(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=200)
    device_type: str


@router.get("/reservations", summary="Held capacity, the rotting ones first")
async def list_reservations(
    status_filter: str | None = Query(None, alias="status"),
    rack_id: str | None = None,
    project: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {
        "items": await res_repo.list_reservations(
            session, status=status_filter, rack_id=rack_id, project=project,
            limit=limit),
        "summary": await res_repo.held_summary(session),
    }


@router.post("/reservations", status_code=status.HTTP_201_CREATED)
async def create_reservation(
    body: ReservationIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    if body.u_start is not None and not body.rack_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "a rack unit range needs a rack")
    if body.u_start is not None and body.u_height is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "a rack unit range needs a height")
    actor = audit.actor_of(principal)
    try:
        reservation_id = await res_repo.create(
            session, project=body.project, owner_group=body.owner_group,
            rack_id=body.rack_id, room_id=body.room_id, u_start=body.u_start,
            u_height=body.u_height, power_kw=body.power_kw,
            cool_kw=body.cool_kw, needed_by=body.needed_by,
            expires_at=body.expires_at, created_by=actor, notes=body.notes)
    except res_repo.ReservationConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "error": "reservation_conflict",
            "message": str(exc),
        }) from None

    ip, agent = audit.client_of(request)
    await audit.record(session, actor=actor, action="reservation.create",
                       target_type="capacity_reservation",
                       target_id=reservation_id, ip=ip, user_agent=agent,
                       before=None,
                       after={"project": body.project,
                              "expires_at": body.expires_at.isoformat()})
    await session.commit()
    return await res_repo.get(session, reservation_id)


@router.post("/reservations/{reservation_id}/release")
async def release_reservation(
    reservation_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    try:
        await res_repo.release(session, reservation_id)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such reservation") from None
    await session.commit()
    return {"status": "released"}


@router.post("/reservations/{reservation_id}/fulfil",
             summary="Turn the held space into the real device")
async def fulfil_reservation(
    reservation_id: str,
    body: FulfilIn,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    """Promotes the placeholder rather than replacing it, so the rack units are
    never briefly free for somebody else's install to slip into."""
    try:
        device_id = await res_repo.fulfil(
            session, reservation_id, name=body.name,
            device_type=body.device_type, actor=audit.actor_of(principal))
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such reservation") from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    await session.commit()
    return {"device_id": device_id}
