"""FastAPI entrypoint.

Hosts the Twilio webhooks, the Media Streams websocket that carries the Pipecat
pipeline, and the operational API (leads, calls, campaigns, cost reports).

Run locally:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
    ngrok http 8000            # PUBLIC_BASE_URL must point at the ngrok URL
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import routes_calls, routes_health, routes_leads, routes_twilio
from app.cache.session import close_session_store
from app.config import get_settings
from app.db.base import create_all, dispose_engine
from app.llm.router import close_router
from app.logging_config import configure_logging, get_logger
from app.observability import setup_tracing, shutdown_tracing

log = get_logger(__name__)

DESCRIPTION = """
Autonomous outbound voice agent: researches a lead, places the call, runs a
tool-driven sales conversation and books a meeting.

* `POST /calls/dial` — dial one lead
* `POST /campaigns/run` — dial every lead that is due
* `GET  /calls/{id}` — transcript, tool calls and cost for a call
* `GET  /reports/costs` — cost per call and cost per booked meeting
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    log.info(
        "app.starting",
        version=__version__,
        env=settings.app_env,
        database="sqlite" if settings.is_sqlite else "postgres",
    )
    setup_tracing()
    if settings.is_sqlite:
        # Postgres deployments run Alembic; SQLite dev/test bootstraps itself.
        await create_all()
    yield
    await close_router()
    await close_session_store()
    await dispose_engine()
    shutdown_tracing()
    log.info("app.stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Vox-Orchestrator",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.app_env != "production" else [],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next):  # noqa: ANN001, ANN202
        started = time.perf_counter()
        response = await call_next(request)
        if request.url.path not in {"/health", "/"}:
            log.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        return response

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN001
        log.error("http.unhandled_error", path=request.url.path, error=str(exc)[:300])
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    app.include_router(routes_health.router)
    app.include_router(routes_leads.router)
    app.include_router(routes_calls.router)
    app.include_router(routes_twilio.router)
    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.app_env == "development",
    )
