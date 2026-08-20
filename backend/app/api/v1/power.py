"""Power analytics endpoints. Routing and validation only."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.services import power as service

router = APIRouter(prefix="/power", tags=["power"])


@router.get("", summary="Fleet power: redundancy census, supply load, imbalance")
async def power_overview(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    r = await service.scope_summary(session)
    return {
        **r,
        "supplies": [asdict(h) for h in r["supplies"]],
    }


@router.get("/chain/{device_id}",
            summary="What feeds this load, and is it still redundant")
async def power_chain(
    device_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """grid -> switchgear -> ATS -> UPS -> PDU -> device, per path.

    ``redundancy`` is the answer an operator needs during an event: N+1,
    single_feed or no_feed. ``reason`` says why, because "single_feed" on a
    server someone believes is dual-corded is only actionable if the response
    explains whether that is one cord or two cords on the same side.
    """
    try:
        uuid.UUID(device_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"device id {device_id!r} is not a UUID") from None
    try:
        chain = await service.chain_for(session, device_id)
    except service.PowerError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    return {
        "device": asdict(chain["device"]),
        "redundancy": chain["redundancy"],
        "reason": chain["reason"],
        "live_paths": chain["live_paths"],
        "total_paths": chain["total_paths"],
        "paths": [
            {
                "side": p.side,
                "healthy": p.healthy,
                "reaches_source": p.reaches_source,
                "hops": [asdict(h) for h in p.hops],
            }
            for p in chain["paths"]
        ],
        # Devices common to every path: the points where 2N stops being 2N.
        "shared_upstream": [asdict(h) for h in chain["shared_upstream"]],
    }
