"""FastAPI application factory.

The API never polls devices and never writes telemetry - both belong to other
processes. What it does own is reads, CRUD, authorisation and the WebSocket
fan-out.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine

log = get_logger("api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(service=settings.service_name)
    log.info("api starting", version=__version__, environment=settings.environment)
    yield
    await dispose_engine()
    log.info("api stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="DCIM Platform API",
        version=__version__,
        description=(
            "Inventory, state, telemetry and collector assignment for the DCIM "
            "platform. Telemetry arrives through the Go collector and the ingest "
            "worker; this service never polls devices."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["ETag"],
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000
        # Query strings can carry a WebSocket ticket; log the path only.
        log.info("request", method=request.method, path=request.url.path,
                 status=response.status_code, duration_ms=round(duration_ms, 1))
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error", path=request.url.path, error=str(exc),
                  exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"type": "https://dcim/errors/internal",
                     "title": "Internal Server Error",
                     "status": 500,
                     "instance": request.url.path},
        )

    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
