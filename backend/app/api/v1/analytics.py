"""Analytics endpoints. Routing and validation only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.repositories import forecast as forecast_repo
from app.services import capacity as capacity_service
from app.services import forecast as forecast_service
from app.services import pue as pue_service
from app.services import thermal as thermal_service

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


@router.get("/thermal", summary="Rack ΔT, hot spots, and CRAH supply vs return")
async def thermal(
    # Typed as UUID so a malformed id is rejected at the edge with a 422.
    # As a plain str it reached the CAST in SQL and came back a 500, which
    # reads as "the server broke" rather than "you sent a bad id".
    room_id: UUID,
    minutes: int = Query(15, ge=1, le=240,
                         description="How long a condition must hold to count"),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Two different findings, deliberately kept apart.

    ``hot_spots`` are racks hot RELATIVE to the room - a local airflow problem.
    ``thermal_event`` is the room drifting up as a whole, which the relative
    test cannot see and which points at plant or capacity instead. A CRAH is
    classified by whether its SUPPLY or its RETURN is high, because those send
    an engineer to opposite ends of the building.
    """
    view = await thermal_service.room_view(session, str(room_id), minutes=minutes)
    if view.get("name") is None:
        # A well-formed id for a room that does not exist. Returning 200 with
        # an empty room says "this room is fine" about a room that is not there.
        raise HTTPException(status_code=404, detail="room not found")
    return view


# Only these can be forecast, because only these have a time series behind
# them. Space and ports are inventory: they change when someone racks a box,
# and the database records their present value, not their history. A trend
# through a single current reading is not a forecast, so asking for one is
# refused rather than answered with a flat line.
_FORECASTABLE = {
    "power": {
        "types": None,          # every real load; filled from the service
        "unit": "kW",
        "label": "coincident load across the scope",
    },
    "it_power": {
        "types": None,
        "unit": "kW",
        "label": "IT load only, which is what the cooling plant must remove",
    },
}


@router.get("/forecast", summary="Capacity trend, runway, and when to refuse")
async def forecast(
    scope: str = Query(..., pattern="^(rack|room|datacenter)$"),
    scope_id: UUID = Query(...),
    metric: str = Query("power", pattern="^(power|it_power)$"),
    horizon_days: int = Query(30, ge=1, le=365),
    history_days: int = Query(90, ge=14, le=730),
    capacity: float | None = Query(
        None, gt=0,
        description="kW limit to measure the runway against. This fleet "
                    "records no rack, PDU or RPP rating, so without one there "
                    "is no runway - see /capacity."),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Project a daily series forward, or explain why it will not.

    The refusal is the feature. Below fourteen days of history this returns
    ``method: insufficient_history`` with no numbers at all - not a wide
    interval, not a provisional value. A capacity plan built on nine days of
    data is worse than one built on none, because it carries a date.
    """
    from app.repositories import capacity as cap_repo

    spec = _FORECASTABLE[metric]
    types = (capacity_service.IT_TYPES if metric == "it_power"
             else capacity_service.LOAD_TYPES)
    name = await cap_repo.scope_name(session, scope=scope,
                                     scope_id=str(scope_id))
    if name is None:
        raise HTTPException(status_code=404, detail=f"{scope} not found")

    mid = await cap_repo.metric_id(session, "power_draw")
    device_ids = await cap_repo.devices_in_scope(
        session, scope=scope, scope_id=str(scope_id), device_types=types)
    daily = await forecast_repo.daily_power(
        session, device_ids=device_ids, metric_id=mid, days=history_days)

    series = daily["series"]
    values = [d["p95_kw"] for d in series]
    result = forecast_service.project(values, horizon_days=horizon_days,
                                      capacity=capacity, unit=spec["unit"])
    result.update({
        "scope": scope, "scope_id": str(scope_id), "name": name,
        "metric": metric, "metric_label": spec["label"],
        "devices": len(device_ids),
        "statistic": "daily p95 of the coincident load",
        "first_day": series[0]["day"] if series else None,
        "last_day": series[-1]["day"] if series else None,
        "history": [{"day": d["day"], "value": round(d["p95_kw"], 2)}
                    for d in series],
    })
    if daily["dropped_days"]:
        result["notes"].append(
            f"{daily['dropped_days']} day(s) were dropped for carrying data "
            f"in fewer than {forecast_repo.MIN_DAY_HOURS} of their 24 hours - "
            f"a day the collector missed half of is an unknown day, not a "
            f"quiet one")
    if daily["gap_days"]:
        result["notes"].append(
            f"{daily['gap_days']} calendar day(s) are missing from the middle "
            f"of the series, and the fit treats the remaining days as evenly "
            f"spaced, so the trend is slightly optimistic about how much time "
            f"the growth took")
    return result
