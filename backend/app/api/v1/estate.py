"""Estate-wide pages: thermal, power, utilisation, and the alert drill-downs.

One request renders one page. Each response carries BOTH the room rows and the
site rows folded from them, because the page is a single table with a scope
toggle - fetching again to switch from sites to rooms would show the reader two
different instants and call it the same screen.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_taxonomy import (
    CATEGORIES,
    DESCRIPTIONS,
    DETECTION_DESCRIPTIONS,
    DETECTIONS,
    RESPONSE_CLASSES,
    RESPONSE_DESCRIPTIONS,
    STRIP_GROUPS,
    examples_for,
)
from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.services import estate as service

router = APIRouter(prefix="/estate", tags=["estate"])


@router.get("/thermal", summary="Intake temperature and compliance by room and site")
async def thermal(
    focus: date | None = Query(None, description="Day to report, UTC. Defaults to yesterday."),
    compare: date | None = Query(
        None, description="Day to compare against. Defaults to the day before focus."),
    mode: str = Query("daily", pattern="^(daily|live)$"),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    """Rack intake air, averaged by SAMPLE across the estate.

    `live` reports the last hour against the hour before it; `daily` reports a
    calendar day in UTC against another. The window is echoed back in the
    response - a temperature without the window it was measured over is not a
    fact anyone can act on.
    """
    return await service.thermal(session, focus=focus, compare=compare, mode=mode)


@router.get("/power", summary="Power split IT / cooling / other, with PUE per row")
async def power(
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    mode: str = Query("average", pattern="^(average|peak)$"),
    live: bool = Query(False, description="Instantaneous draw instead of a window"),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    if start and end and start >= end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start must precede end")
    if start and start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end and end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return await service.power(session, start=start, end=end, mode=mode, live=live)


@router.get("/utilization", summary="Space, power and cooling used against installed")
async def utilization(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    """Utilisation is always "now": it is a state of the estate, not a window.

    Every percentage carries the basis of its denominator, because "62% power"
    means something different against a design rating than against the summed
    nameplate of whatever PDUs happen to be installed.
    """
    return await service.utilisation(session)


@router.get("/alert-categories", summary="The taxonomy itself: categories, "
                                        "owners, detection methods")
async def alert_categories(
    _: Principal = Depends(current_principal),
) -> dict:
    """What each counter means, served from the classifier that fills it.

    The UI legend is generated from this rather than written beside it, so the
    definition an operator reads and the rule the classifier applies cannot
    drift - and a category added to the taxonomy appears in the legend without
    a frontend change.
    """
    return {
        "categories": [{
            "key": c,
            "label": DESCRIPTIONS[c]["label"],
            "owner": DESCRIPTIONS[c]["owner"],
            "description": DESCRIPTIONS[c]["text"],
            # Real entries out of the classifier, not illustrations of it.
            "examples": examples_for(c),
        } for c in CATEGORIES],
        # How the eight group into the five headline counters on the home
        # strip. The table keeps one column per category, so the grouping is a
        # presentation of the same numbers and hides nothing.
        "strip_groups": [{
            "key": key, "label": label, "categories": list(members),
        } for key, label, members in STRIP_GROUPS],
        "detections": [{
            "key": d,
            "label": DETECTION_DESCRIPTIONS[d]["label"],
            "description": DETECTION_DESCRIPTIONS[d]["text"],
        } for d in DETECTIONS],
        # Alarm or alert - required response, the ISA-18.2 split. An attribute,
        # like detection: it cuts across all eight categories.
        "response_classes": [{
            "key": c,
            "label": RESPONSE_DESCRIPTIONS[c]["label"],
            "description": RESPONSE_DESCRIPTIONS[c]["text"],
        } for c in RESPONSE_CLASSES],
    }


@router.get("/alerts", summary="Open alerts of one category, by room")
async def alerts(
    category: str = Query(..., description="One of: " + ", ".join(CATEGORIES)),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    if category not in CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"unknown category {category!r}")
    return await service.alerts(session, category=category)


@router.get("/rooms/{room_id}/kpi", summary="Everything the room drawer shows")
async def room_kpi(
    room_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    result = await service.room_kpi(session, room_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such room")
    return result
