"""Topology endpoints. Routing and validation only - logic lives in the service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.schemas import TopologyOut
from app.services import topology as service

router = APIRouter(prefix="/topology", tags=["topology"])


@router.get("", response_model=TopologyOut, summary="Per-layer topology graph")
async def get_topology(
    layer: str = Query(
        "power",
        description="network (alias: production) | management | power | cooling | fieldbus",
    ),
    scope: str = Query(
        ...,
        description="Anchor as '<type>:<id>': datacenter, room, rack or device",
        examples=["room:8f1c2b7e-0a4d-4c31-9f77-2c9a1b6d5e10"],
    ),
    depth: int = Query(
        1, ge=0, le=4,
        description=(
            "Hops out from the anchor. 0 returns only the devices in the scope "
            "and the edges between them; 1 also returns what feeds them, which "
            "for power and cooling is usually in another room entirely."
        ),
    ),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> TopologyOut:
    try:
        return await service.get_topology(
            session, layer=layer, scope=scope, depth=depth)
    except service.TopologyError as exc:
        # The service raises this only for input the caller can fix, and its
        # message says how - so pass it through rather than flattening it to a
        # generic 400.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
