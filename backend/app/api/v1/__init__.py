"""API v1 router aggregation."""

from fastapi import APIRouter

from app.api.v1 import collector, devices, infrastructure, misc

api_router = APIRouter()
api_router.include_router(misc.router)
api_router.include_router(devices.router)
api_router.include_router(infrastructure.router)
api_router.include_router(collector.router)

__all__ = ["api_router"]
