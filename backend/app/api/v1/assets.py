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


@router.get("/filter-options", summary="Vocabularies for the inventory filters")
async def filter_options(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return await service.filter_options(session)
