"""Capacity endpoints. Routing and validation only."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.services import capacity as service

router = APIRouter(prefix="/capacity", tags=["capacity"])


@router.get("", summary="Power, cooling, space and ports, and which binds")
async def capacity(
    scope: str = Query(..., pattern="^(rack|room|datacenter)$"),
    scope_id: str = Query(...),
    hours: int = Query(720, ge=1, le=8760,
                       description="Window for the load percentile"),
    assumed_rack_kw: float | None = Query(
        None, gt=0,
        description="Design kW per rack, if inventory does not record it. "
                    "Applied as an assumption and labelled as one."),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """All four constraints, with the binding one flagged.

    A constraint whose limit is not recorded reports its usage and says the
    limit is unknown, rather than being treated as unlimited - so the binding
    answer is never quietly wrong about what it could not see.
    """
    try:
        uuid.UUID(scope_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"scope_id {scope_id!r} is not a UUID") from None
    return await service.report(session, scope=scope, scope_id=scope_id,
                                hours=hours, assumed_rack_kw=assumed_rack_kw)
