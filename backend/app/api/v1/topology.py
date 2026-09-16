"""Topology endpoints. Routing and validation only - logic lives in the service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.schemas import ImpactOut, TopologyOut, TraceOut
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
    rollup: str = Query(
        "none",
        description=(
            "'rack' collapses each rack's LEAF equipment - devices that feed "
            "nothing else on this layer - into one node per rack and device "
            "type, merging their edges and carrying the count. A server hall's "
            "power layer is 800 boxes without it and 60 with it. Applied after "
            "the node cap, so a scope large enough to truncate still says so."
        ),
    ),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> TopologyOut:
    try:
        return await service.get_topology(
            session, layer=layer, scope=scope, depth=depth, rollup=rollup)
    except service.TopologyError as exc:
        # The service raises this only for input the caller can fix, and its
        # message says how - so pass it through rather than flattening it to a
        # generic 400.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/trace/{device_id}", response_model=TraceOut,
            summary="The chain from this device back to its source")
async def get_trace(
    device_id: str,
    layer: str = Query(
        "power",
        description="network (alias: production) | management | power | cooling | fieldbus",
    ),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> TraceOut:
    """What someone about to unplug a cord needs to know about the far end.

    Upstream only. "What hangs off this" is answered properly by
    ``/topology/impact/{id}`` - what goes dark versus what merely loses a
    redundancy side - and enumerating downstream routes here would answer
    neither question. The immediate neighbours come along because "what is
    plugged into this" is one hop, not a path.
    """
    try:
        return await service.get_trace(session, device_id=device_id, layer=layer)
    except service.TopologyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None


@router.get("/impact/{device_id}", response_model=ImpactOut,
            summary="What breaks if this device is removed")
async def get_impact(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> ImpactOut:
    """Answers the question asked before every maintenance window.

    ``cut_off`` is the list that matters: devices left with no surviving path
    from any source. ``degraded`` is the softer outcome - still served, but
    down to fewer redundancy sides, which is the normal and usually accepted
    cost of taking one side of a 2N distribution out.
    """
    try:
        return await service.get_impact(session, device_id)
    except service.TopologyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
