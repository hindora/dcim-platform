"""The commissioning queue: devices whose wire has moved ahead of their record.

Read-only on purpose, and there are two reasons rather than one.

Confirming a row is an ordinary lifecycle transition, so the button posts to
`/devices/{id}/lifecycle` and gets the matrix check, the event and the audit row
that every other transition gets. A second write path here would be a second set
of rules to keep in step.

And nothing here reaches out to the device plane. This router briefly carried a
"sync" that logged into the simulator's REST API and read its topology export -
backend to backend, one product reading another's database through its front
door. The DCIM must not know what is generating its telemetry. Finding hardware
is discovery's job, over SNMP/Redfish/gNMI on the management network, which is
where the collector already does it; the importer is a fixture loader for seeding
a development estate and belongs on a command line, not in an operator's UI.
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

