"""The commissioning queue: devices whose wire has moved ahead of their record.

Read-only on purpose. Confirming a row is an ordinary lifecycle transition, so
the button posts to `/devices/{id}/lifecycle` and gets the matrix check, the
event and the audit row that every other transition gets. A second write path
here would be a second set of rules to keep in step.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.repositories import commissioning as repo

router = APIRouter(prefix="/commissioning", tags=["commissioning"])


@router.get("/queue", summary="Devices racked, built or answering when they should not be")
async def queue(
    soak_hours: int = Query(repo.DEFAULT_SOAK_HOURS, ge=0, le=720, description=(
        "Hours a machine must have been `installed` before acceptance is "
        "offered. A burn-in is 24-48h; lower it to see everything.")),
    limit: int = Query(500, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    items = await repo.ready_queue(session, soak_hours=soak_hours, limit=limit)
    return {
        "soak_hours": soak_hours,
        "counts": await repo.counts(session, soak_hours=soak_hours),
        "items": items,
    }
