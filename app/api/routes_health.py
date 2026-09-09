"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app import __version__
from app.api.schemas import HealthResponse
from app.cache.session import get_session_store
from app.config import get_settings
from app.db.base import session_scope
from app.llm.router import get_router
from app.pipeline.pipecat_runner import pipecat_available
from app.stt import STTRouter
from app.telephony.compliance import in_calling_window
from app.telephony.twilio_client import get_telephony
from app.tts import TTSRouter

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness + dependency status")
async def health() -> HealthResponse:
    settings = get_settings()

    database = "ok"
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        database = f"error: {str(exc)[:120]}"

    store = get_session_store()
    cache = "ok" if await store.ping() else "degraded"
    if not settings.redis_url:
        cache = "memory-fallback"

    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        version=__version__,
        environment=settings.app_env,
        database=database,
        cache=cache,
        providers={
            "llm": get_router().active_providers(),
            "stt_configured": STTRouter().is_configured(),
            "tts_configured": TTSRouter().is_configured(),
            "twilio_configured": get_telephony().is_configured(),
        },
        pipecat_installed=pipecat_available(),
        compliance={
            "dlt_registered": settings.compliance_dlt_registered,
            "dnc_check_enabled": settings.compliance_dnc_check_enabled,
            "inside_calling_window": in_calling_window(),
        },
    )


@router.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": "vox-orchestrator",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
    }
