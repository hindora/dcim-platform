"""Suppliers, support contracts and tags."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.repositories import contracts as repo
from app.repositories import tags as tag_repo
from app.services import contracts as service

router = APIRouter(tags=["contracts"])


# ---------------------------------------------------------------- suppliers

class SupplierIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    account_ref: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    notes: str | None = None


@router.get("/suppliers", summary="Who we buy from and who supports it")
async def list_suppliers(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {"items": await repo.list_suppliers(session)}


@router.post("/suppliers", status_code=status.HTTP_201_CREATED)
async def create_supplier(
    body: SupplierIn,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    supplier_id = await repo.create_supplier(session, **body.model_dump())
    await session.commit()
    return {"id": supplier_id}


@router.delete("/suppliers/{supplier_id}")
async def delete_supplier(
    supplier_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    try:
        await repo.delete_supplier(session, supplier_id)
        await session.commit()
    except Exception as exc:  # FK from device.supplier_id or support_contract
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "error": "referenced",
            "message": "this supplier still has devices or contracts against it",
        }) from exc
    return {"status": "deleted"}


# ---------------------------------------------------------------- contracts

class ContractIn(BaseModel):
    model_config = {"extra": "forbid"}

    supplier_id: str | None = None
    reference: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern="^(warranty|support|maintenance)$")
    service_level: str | None = Field(None, max_length=100)
    start_date: date
    end_date: date
    cost: float | None = None
    currency: str | None = Field(None, min_length=3, max_length=3)
    auto_renew: bool = False
    notes: str | None = None
    device_ids: list[str] = Field(default_factory=list)


class ContractPatch(BaseModel):
    model_config = {"extra": "forbid"}

    supplier_id: str | None = None
    reference: str | None = None
    kind: str | None = Field(None, pattern="^(warranty|support|maintenance)$")
    service_level: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    cost: float | None = None
    currency: str | None = None
    auto_renew: bool | None = None
    notes: str | None = None


class DevicesBody(BaseModel):
    model_config = {"extra": "forbid"}
    device_ids: list[str]


@router.get("/contracts", summary="Support contracts, soonest expiry first")
async def list_contracts(
    kind: str | None = None,
    supplier_id: str | None = None,
    state: str | None = Query(None, pattern="^(active|expiring|expired)$"),
    limit: int = Query(200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {
        "items": await repo.list_contracts(
            session, kind=kind, supplier_id=supplier_id, state=state, limit=limit),
        # The threshold behind `state`, served rather than assumed, so the UI
        # can say "expiring within 90 days" without hard-coding 90.
        "expiring_days": service.EXPIRING_DAYS,
    }


@router.post("/contracts", status_code=status.HTTP_201_CREATED)
async def create_contract(
    body: ContractIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    fields = body.model_dump(exclude={"device_ids"})
    try:
        contract = await service.create(session, fields, body.device_ids)
    except service.ContractError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None

    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal),
                       action="contract.create", target_type="support_contract",
                       target_id=contract["id"], ip=ip, user_agent=agent,
                       before=None,
                       after={"reference": body.reference, "kind": body.kind,
                              "end_date": body.end_date.isoformat(),
                              "devices": len(body.device_ids)})
    await session.commit()
    return contract


@router.get("/contracts/{contract_id}")
async def get_contract(
    contract_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    contract = await repo.get_contract(session, contract_id)
    if contract is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such contract")
    contract["devices"] = await repo.contract_devices(session, contract_id)
    return contract


@router.patch("/contracts/{contract_id}")
async def update_contract(
    contract_id: str,
    body: ContractPatch,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    try:
        contract = await service.update(session, contract_id, changes)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such contract") from None
    except service.ContractError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    await session.commit()
    return contract


@router.delete("/contracts/{contract_id}")
async def delete_contract(
    contract_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, Any]:
    affected = await service.delete(session, contract_id)
    await session.commit()
    return {"status": "deleted", "devices_recomputed": affected}


@router.get("/contracts/{contract_id}/devices")
async def contract_devices(
    contract_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {"items": await repo.contract_devices(session, contract_id)}


@router.post("/contracts/{contract_id}/devices",
             summary="Put devices under this contract")
async def cover_devices(
    contract_id: str,
    body: DevicesBody,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, int]:
    try:
        added = await service.cover(session, contract_id, body.device_ids)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such contract") from None
    await session.commit()
    return {"added": added}


@router.delete("/contracts/{contract_id}/devices/{device_id}")
async def uncover_device(
    contract_id: str,
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    await service.uncover(session, contract_id, device_id)
    await session.commit()
    return {"status": "removed"}


# --------------------------------------------------------------------- tags

class TagIn(BaseModel):
    model_config = {"extra": "forbid"}

    key: str = Field(min_length=1, max_length=60)
    value: str = Field(min_length=1, max_length=100)
    colour: str | None = Field(None, max_length=32)
    description: str | None = None


class TagPatch(BaseModel):
    model_config = {"extra": "forbid"}

    key: str | None = None
    value: str | None = None
    colour: str | None = None
    description: str | None = None


class TagAssign(BaseModel):
    model_config = {"extra": "forbid"}
    tag_ids: list[str]


@router.get("/tags", summary="The controlled vocabulary, with usage counts")
async def list_tags(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {"items": await tag_repo.list_tags(session)}


@router.post("/tags", status_code=status.HTTP_201_CREATED)
async def create_tag(
    body: TagIn,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    try:
        tag_id = await tag_repo.create_tag(session, **body.model_dump())
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "error": "tag_exists",
            "message": f"{body.key}={body.value} already exists",
        }) from exc
    return {"id": tag_id}


@router.patch("/tags/{tag_id}")
async def update_tag(
    tag_id: str,
    body: TagPatch,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    await tag_repo.update_tag(session, tag_id, body.model_dump(exclude_unset=True))
    await session.commit()
    return {"status": "updated"}


@router.delete("/tags/{tag_id}",
               summary="Delete a tag; objects keep existing, they lose the label")
async def delete_tag(
    tag_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    await tag_repo.delete_tag(session, tag_id)
    await session.commit()
    return {"status": "deleted"}


@router.get("/objects/{object_type}/{object_id}/tags")
async def object_tags(
    object_type: str,
    object_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    try:
        return {"items": await tag_repo.tags_for(session, object_type, object_id)}
    except tag_repo.UnknownObjectTypeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None


@router.post("/objects/{object_type}/{object_id}/tags")
async def assign_tags(
    object_type: str,
    object_id: str,
    body: TagAssign,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("operator")),
) -> dict[str, int]:
    try:
        added = await tag_repo.assign(
            session, object_type=object_type, object_id=object_id,
            tag_ids=body.tag_ids, actor=audit.actor_of(principal))
    except tag_repo.UnknownObjectTypeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    await session.commit()
    return {"added": added}


@router.delete("/objects/{object_type}/{object_id}/tags/{tag_id}")
async def unassign_tag(
    object_type: str,
    object_id: str,
    tag_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_role("operator")),
) -> dict[str, str]:
    try:
        await tag_repo.unassign(session, object_type=object_type,
                                object_id=object_id, tag_id=tag_id)
    except tag_repo.UnknownObjectTypeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    await session.commit()
    return {"status": "removed"}
