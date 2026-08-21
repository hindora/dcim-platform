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
from fastapi.responses import JSONResponse, Response

from app import __version__
from app.api.v1 import api_router
from app.core import metrics
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine
from app.services import topology as topology_service
from app.websocket.hub import ConnectionHub, set_hub

log = get_logger("api")


@asynccontextmanager


async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(service=settings.service_name)
    log.info("api starting", version=__version__, environment=settings.environment)

    # One hub per process, with a single Redis pattern subscription behind it.
    from redis.asyncio import Redis

    redis = Redis.from_url(settings.redis_url)
    hub = ConnectionHub(redis, coalesce_ms=settings.ws_coalesce_ms,
                        max_topics=settings.ws_max_topics)
    await hub.start()
    set_hub(hub)

    yield

    await hub.stop()
    set_hub(None)
    await topology_service.close_cache()
    await redis.aclose()
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
        elapsed = time.perf_counter() - started
        # Query strings can carry a WebSocket ticket; log the path only.
        log.info("request", method=request.method, path=request.url.path,
                 status=response.status_code,
                 duration_ms=round(elapsed * 1000, 1))
        # The TEMPLATED path, resolved after routing: /devices/{id}, never
        # /devices/2f9c-... . Labelling with the raw path gives one time series
        # per device and takes the Prometheus install down with it.
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "unmatched"
        metrics.api_requests.labels(method=request.method, path=path,
                                    status=str(response.status_code)).inc()
        metrics.api_duration.labels(method=request.method, path=path).observe(elapsed)
        return response

    # Deliberately outside the /api/v1 prefix and unauthenticated, matching the
    # collector's own :9100/metrics. Prometheus scrapers do not carry JWTs, and
    # the metrics here are cardinality-disciplined enough to leak nothing about
    # individual devices. It still belongs on an internal network - this is a
    # deployment control, not an application one.
    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        from app.db.session import get_engine
        try:
            pool = get_engine().pool
            metrics.db_pool.labels(state="in_use").set(pool.checkedout())
            metrics.db_pool.labels(state="idle").set(pool.checkedin())
        except Exception:  # pragma: no cover - pool introspection is best effort
            pass
        return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)

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
