"""Device credentials: list, create, rotate (docs/26 Phase 4).

Responses carry `secret_hint` only - never a secret. Creating and rotating
are admin: a credential reaches every collector that polls with it, and a
wrong one is an authentication failure across a whole device class at once.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.logging import get_logger
from app.core.security import Principal, current_principal, require_role
from app.db.session import get_session
from app.services import credentials as service

router = APIRouter(prefix="/credentials", tags=["credentials"])
log = get_logger("api.credentials")


class CreateBody(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1, max_length=128)
    kind: str = Field(..., description="snmp_v2c | snmp_v3 | http_basic")
    #: snmp_v3: security_name, auth_protocol, auth_key, priv_protocol, priv_key
    secret: dict[str, Any]


class RotateBody(BaseModel):
    model_config = {"extra": "forbid"}

    secret: dict[str, Any]


@router.get("", summary="Credentials, with what uses them - hints only")
async def list_credentials(
    q: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return {"credentials": await service.listing(session, q, kind, max(1, min(limit, 500)))}


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create a credential")
async def create_credential(
    body: CreateBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    try:
        row = await service.create(session, body.name, body.kind, body.secret)
    except service.CredentialError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="credential.create",
                       target_type="credential", target_id=row["id"], ip=ip, user_agent=agent,
                       after={"name": row["name"], "kind": row["kind"],
                              "hint": row["secret_hint"]})
    await session.commit()
    log.info("credential created", credential_id=row["id"], kind=row["kind"],
             actor=principal.username)
    return row


@router.post("/{credential_id}/rotate", summary="Replace a credential's secret")
async def rotate_credential(
    credential_id: str,
    body: RotateBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_role("admin")),
) -> dict[str, Any]:
    """Every endpoint and pool using it gets the new secret on its collector's
    next assignment fetch - nothing else needs touching."""
    try:
        row = await service.rotate(session, credential_id, body.secret)
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such credential") from None
    except service.CredentialError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    ip, agent = audit.client_of(request)
    await audit.record(session, actor=audit.actor_of(principal), action="credential.rotate",
                       target_type="credential", target_id=credential_id, ip=ip,
                       user_agent=agent, after={"hint": row["secret_hint"]})
    await session.commit()
    log.warning("credential rotated", credential_id=credential_id, actor=principal.username)
    return row
