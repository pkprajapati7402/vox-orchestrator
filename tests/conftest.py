"""Shared pytest fixtures: an isolated SQLite database and settings overrides."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("TWILIO_VALIDATE_SIGNATURES", "false")
os.environ.setdefault("LLM_PRIMARY_PROVIDER", "mock")
os.environ.setdefault("LLM_FALLBACK_PROVIDER", "none")
os.environ.setdefault("EVAL_LLM_PROVIDER", "mock")
os.environ.setdefault("COMPLIANCE_DNC_CHECK_ENABLED", "true")
os.environ.setdefault("CALLING_WINDOW_START_HOUR", "0")
os.environ.setdefault("CALLING_WINDOW_END_HOUR", "24")


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture()
async def db(tmp_path: Path) -> AsyncIterator[None]:
    """Fresh SQLite database per test, wired into the app's engine singleton."""
    from app.config import reload_settings
    from app.db import base as db_base

    db_path = tmp_path / "test.sqlite3"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    reload_settings()
    await db_base.dispose_engine()
    await db_base.create_all()
    try:
        yield
    finally:
        await db_base.dispose_engine()
        os.environ.pop("DATABASE_URL", None)
        reload_settings()


@pytest.fixture()
def settings():  # noqa: ANN201
    from app.config import get_settings

    return get_settings()


@pytest.fixture()
def lead_context():  # noqa: ANN201
    from app.pipeline.engine import LeadContext

    return LeadContext(
        business_name="Glow Studio Salon",
        phone="+911140001001",
        category="salon",
        address="Hauz Khas, New Delhi",
        contact_name="Ritu",
        research_notes="Rated 4.6 with 320 reviews; site highlights bridal packages.",
    )


@pytest.fixture()
def mock_llm():  # noqa: ANN201
    from app.llm.mock import MockLLMClient

    return MockLLMClient()
