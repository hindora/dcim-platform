"""Cooling analytics endpoints. Routing and validation only."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.services import cooling as service

router = APIRouter(prefix="/cooling", tags=["cooling"])


def _chiller(c) -> dict[str, Any]:
    return {
        "device_id": c.device_id, "name": c.name, "status": c.status,
        "running": c.running,
        "rated_kw": c.rated_kw,
        "compressor_load_pct": c.compressor_load_pct,
        "power_kw": c.power_kw, "cop": c.cop,
        # Both estimates, and how far apart they are. Reporting one and hiding
        # the other would turn a sensor fault into a confident number.
        "output_thermal_kw": c.output_thermal_kw,
        "output_electrical_kw": c.output_electrical_kw,
        "output_disagreement_pct": c.output_disagreement_pct,
        "load_pct": c.load_pct,
        "chw": asdict(c.chw) | {"delta_t_k": c.chw.delta_t_k,
                                "heat_kw": c.chw.heat_kw,
                                "low_delta_t": c.chw.low_delta_t} if c.chw else None,
        "cond": asdict(c.cond) | {"delta_t_k": c.cond.delta_t_k,
                                  "heat_kw": c.cond.heat_kw} if c.cond else None,
    }


@router.get("", summary="Plant state: staging, capacity against load, loop ΔT")
async def cooling_overview(
    room_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    r = await service.plant_view(session, room_id)
    plant = r.pop("plant")
    return {
        **r,
        "chillers": [_chiller(c) for c in plant.chillers],
        "loops": [
            asdict(loop) | {"delta_t_k": loop.delta_t_k, "heat_kw": loop.heat_kw,
                            "low_delta_t": loop.low_delta_t}
            for loop in plant.loops
        ],
    }


@router.get("/plant/{room_id}", summary="CHW loop detail for one room")
async def cooling_plant(
    room_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict[str, Any]:
    return await cooling_overview(room_id=room_id, session=session, _=_)
