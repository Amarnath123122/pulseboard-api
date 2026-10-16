import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api import health, internal, public, realtime
from app.assistants import setup
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging, request_id_ctx
from app.db.models import Base
from app.db.seed import seed_reference_data
from app.db.session import db
from app.services.events import hub
from app.services.workers import review_scheduler, review_worker

log = logging.getLogger("app")


def _enable_app_insights() -> None:
    """Send traces, logs and metrics to Azure Application Insights when a connection string is present."""
    if not os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        logging.getLogger("app").warning("azure-monitor-opentelemetry not installed; telemetry disabled")
        return
    configure_azure_monitor(logger_name="app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if db.engine is None:
        db.init(settings.database_url)
    if settings.db_auto_create:
        async with db.engine.begin() as conn:  # type: ignore[union-attr]
            await conn.run_sync(Base.metadata.create_all)
    async with db.session() as session:
        await seed_reference_data(session)

    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    if settings.uses_postgres:
        tasks.append(asyncio.create_task(hub.run_relay(stop)))
    if settings.run_background_workers:
        tasks += [asyncio.create_task(review_worker(stop)), asyncio.create_task(review_scheduler(stop))]
    log.info("started", extra={"ctx": {"env": settings.environment, "background_tasks": len(tasks)}})
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await setup.drain()
        await db.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    _enable_app_insights()
    app = FastAPI(
        title="Pulseboard API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # noqa: ANN001, ANN202
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = request_id_ctx.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        log.info("request", extra={"ctx": {
            "request_id": rid, "method": request.method, "path": request.url.path,
            "status": response.status_code, "ms": round((time.perf_counter() - start) * 1000, 1),
        }})
        return response

    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(public.router)
    app.include_router(internal.router)
    app.include_router(realtime.router)
    return app


app = create_app()
