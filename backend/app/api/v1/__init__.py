"""API v1 router aggregation."""

from fastapi import APIRouter

from app.api.v1 import (
    alarms,
    analytics,
    assets,
    capacity,
    collector,
    collectors,
    contracts,
    cooling,
    devices,
    discovery,
    estate,
    infrastructure,
    inventory,
    maintenance,
    misc,
    power,
    profiles,
    sites,
    topology,
    ws,
)

api_router = APIRouter()
api_router.include_router(misc.router)
api_router.include_router(devices.router)
api_router.include_router(assets.router)
api_router.include_router(maintenance.router)
api_router.include_router(contracts.router)
api_router.include_router(inventory.router)
api_router.include_router(infrastructure.router)
api_router.include_router(analytics.router)
api_router.include_router(capacity.router)
api_router.include_router(cooling.router)
api_router.include_router(power.router)
api_router.include_router(profiles.router)
api_router.include_router(sites.router)
api_router.include_router(estate.router)
api_router.include_router(topology.router)
api_router.include_router(discovery.router)
api_router.include_router(alarms.router)
api_router.include_router(collector.router)
api_router.include_router(collectors.router)
api_router.include_router(ws.router)

__all__ = ["api_router"]
