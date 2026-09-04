"""Asset workspace endpoints.

Read-only in phase 1, and deliberately few: the asset list is `/devices` with
extra filters (docs/21 §2), not a second resource returning a different object.
What lives here is what `/devices` cannot answer - estate-wide counts, and the
vocabularies the filter rail needs.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.repositories import snapshots as snapshot_repo
from app.services import assets as service

router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("/summary", summary="Estate-wide asset counts for the landing page")
async def assets_summary(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """One call behind the whole /assets overview.

    Blocks that need tables not yet migrated - warranty, maintenance, parts -
    are ABSENT rather than present and zero. A tile reading "0 contracts
    expiring" when no contract table exists is a statement an operator would
    act on, and it would be false.

    `identity.unidentified` reads the whole estate today. That is docs/19 B2
    put where somebody sees it rather than left in a document.
    """
    return await service.summary(session)


@router.get("/charts", summary="Composition and capacity, for the overview")
async def assets_charts(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Every chart on the overview, in one round trip.

    Counts only - nothing here is a time series, because nothing in this
    schema records history yet. A trend needs either lifecycle events to
    accrue or a nightly snapshot, and drawing one from a single point would be
    a line that says something it cannot know.
    """
    return await service.charts(session)


@router.get("/trends", summary="The estate over time, from the nightly snapshots")
async def assets_trends(
    days: int = 90,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    """Snapshots for the state trends, lifecycle events for the activity.

    Two sources on purpose: a snapshot DIFF conflates movement - ten installs
    and ten decommissions in one day net to zero - so counts of state come from
    the snapshots and movements between states come from the events.
    """
    days = min(days, 400)
    return {
        "snapshots": await snapshot_repo.series(session, days=days),
        # The activity window follows the range, so the two halves of the
        # section describe the same stretch of time.
        "activity": await snapshot_repo.lifecycle_activity(
            session, months=max(1, min(13, round(days / 30)))),
    }


@router.get("/filter-options", summary="Vocabularies for the inventory filters")
async def filter_options(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return await service.filter_options(session)
