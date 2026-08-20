"""Analytics endpoints. Routing and validation only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.services import pue as pue_service

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/pue", summary="Power Usage Effectiveness over a window")
async def pue(
    datacenter_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Energy-based where the meters allow it, power-based where they do not.

    ``method`` and ``category`` are part of the answer, not decoration: an
    energy PUE and an instantaneous power ratio are different claims, and the
    same site reports a different number at Category 1 than at Category 3.
    """
    end = end or datetime.now(UTC)
    start = start or (end - timedelta(hours=1))
    if start >= end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "start must be before end")
    return await pue_service.compute(session, start=start, end=end,
                                     datacenter_id=datacenter_id)


@router.get("/pue/series", summary="PUE per bucket across a window")
async def pue_series(
    datacenter_id: str | None = None,
    hours: int = Query(12, ge=2, le=168),
    bucket_hours: int = Query(1, ge=1, le=24),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """One PUE per bucket.

    A single figure hides the shape: PUE climbs at low IT load because the
    facility overhead is largely fixed, so a site looks worse at 3am for
    reasons that have nothing to do with efficiency.
    """
    end = datetime.now(UTC)
    step = timedelta(hours=bucket_hours)
    points = []
    cursor = end - timedelta(hours=hours)
    while cursor < end:
        nxt = min(cursor + step, end)
        r = await pue_service.compute(session, start=cursor, end=nxt,
                                      datacenter_id=datacenter_id)
        points.append({"start": cursor, "end": nxt, "pue": r["pue"],
                       "method": r["method"]})
        cursor = nxt
    usable = [p["pue"] for p in points if p["pue"]]
    return {
        "points": points,
        "buckets": len(points),
        "mean": round(sum(usable) / len(usable), 3) if usable else None,
    }
