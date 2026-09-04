"""Asset-view services. Thin: the questions are aggregates, so the SQL is the
logic and it lives in the repository."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import assets as repo


async def summary(session: AsyncSession) -> dict[str, Any]:
    data = await repo.summary(session)

    # Derived here rather than in the browser so the tile, the filter and any
    # later report cannot disagree about what "free" means.
    estate = data["estate"]
    estate["u_free"] = max(
        0, estate["u_total"] - estate["u_used"] - estate["u_reserved"])

    return data


async def charts(session: AsyncSession) -> dict[str, Any]:
    return await repo.charts(session)


async def filter_options(session: AsyncSession) -> dict[str, Any]:
    """Everything the inventory filter rail needs, in one call.

    One call rather than three because the rail renders as a unit: three
    round-trips means three loading states stacked on one panel.
    """
    return {
        "device_types": await repo.device_types(session),
        "vendors": await repo.vendors(session),
        "sites": await repo.sites(session),
        "rooms": await repo.rooms_by_site(session),
        "owner_groups": await repo.owner_groups(session),
        # Served from the server so the UI never hard-codes a state the
        # database does not have. Migration 0043 added the middle three.
        "lifecycles": [
            {"value": "planned", "label": "Planned"},
            {"value": "in_stock", "label": "In stock"},
            {"value": "installed", "label": "Installed"},
            {"value": "in_service", "label": "In service"},
            {"value": "maintenance", "label": "Maintenance"},
            {"value": "decommissioned", "label": "Decommissioned"},
            {"value": "retired", "label": "Retired"},
        ],
    }
