"""API v1 router aggregation."""

from fastapi import APIRouter

from app.api.v1 import (
    alarms,
    analytics,
    assets,
    bulk,
    capacity,
    collector,
    collectors,
    commissioning,
    contracts,
    cooling,
    devices,
    discovery,
    estate,
    infrastructure,
    integrations,
    inventory,
    maintenance,
    misc,
    power,
    profiles,
    sites,
    topology,
    webhooks,
    ws,
)

api_router = APIRouter()
api_router.include_router(misc.router)
api_router.include_router(devices.router)
api_router.include_router(assets.router)
api_router.include_router(bulk.router)
api_router.include_router(maintenance.router)
api_router.include_router(commissioning.router)
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
api_router.include_router(integrations.router)
# Its own router because it is the one endpoint a stranger can
# reach: kept apart so the auth dependency every other route
# carries cannot be added here, or removed there, by accident.
api_router.include_router(webhooks.router)
api_router.include_router(ws.router)

__all__ = ["api_router"]
