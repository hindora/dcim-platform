"""Home-page site overview and the per-site KPI panel."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.services import sites as service

router = APIRouter(prefix="/sites", tags=["sites"])


@router.get("/overview", summary="Every site with its rooms and alert roll-up")
async def sites_overview(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    """One call behind the whole home page.

    Sites, their rooms, per-category alert counts and the fleet totals for the
    alert strip. Deliberately not a fan-out: this is the page that stays open
    on a wall display.
    """
    return await service.overview(session)


@router.get("/platform/state", summary="Whether the monitoring can be trusted")
async def platform_state(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    """The trust banner's own source, deliberately separate from the overview.

    This answers one question - can the platform still see the estate - and it
    is asked from every page, including the ones that never load a site. Two
    queries rather than the whole home-page rollup, so a banner shown on the
    assets list does not cost a fleet-wide aggregate.

    Keeping it off /sites/overview matters for a second reason: if that call
    fails, the banner has to still work. A trust indicator that disappears
    when the estate query breaks is missing at exactly the moment its subject
    is most likely true.
    """
    return await service.platform_health(session)


@router.get("/{datacenter_id}/kpi", summary="Efficiency, load and utilisation for one site")
async def site_kpi(
    datacenter_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    result = await service.kpi(session, datacenter_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such datacenter")
    return result
