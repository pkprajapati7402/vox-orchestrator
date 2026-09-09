"""LLM router: bounded retries against the primary provider, then failover.

Implements the two LLM rows of the retry matrix (Project-Details.md §7):
  * "LLM API timeout or outage" -> fall back to the secondary provider
  * transient errors            -> retry `LLM_MAX_RETRIES` times with backoff
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import get_settings
from app.llm.base import LLMClient, LLMError, LLMResponse, Message
from app.logging_config import get_logger

log = get_logger(__name__)


def build_client(provider: str) -> LLMClient | None:
    """Instantiate a provider client by name (`groq`, `gemini`, `mock`, `none`)."""
    provider = (provider or "").lower()
    if provider in {"", "none"}:
        return None
    if provider == "groq":
        from app.llm.groq_client import GroqLLMClient

        return GroqLLMClient()
    if provider == "gemini":
        from app.llm.gemini_client import GeminiLLMClient

        return GeminiLLMClient()
    if provider == "mock":
        from app.llm.mock import MockLLMClient

        settings = get_settings()
        return MockLLMClient(agent_name=settings.agent_name, agency_name=settings.agency_name)
    raise ValueError(f"unknown LLM provider: {provider!r}")


class LLMRouter(LLMClient):
    """Fronts one or more `LLMClient`s and hides provider failures from callers."""

    provider = "router"

    def __init__(
        self,
        primary: LLMClient | None = None,
        fallback: LLMClient | None = None,
        *,
        max_retries: int | None = None,
        retry_base_delay: float = 0.25,
    ) -> None:
        settings = get_settings()
        self.primary = (
            primary if primary is not None else build_client(settings.llm_primary_provider)
        )
        self.fallback = (
            fallback if fallback is not None else build_client(settings.llm_fallback_provider)
        )
        self.max_retries = settings.llm_max_retries if max_retries is None else max_retries
        self.retry_base_delay = retry_base_delay
        self.stats: dict[str, int] = {"calls": 0, "retries": 0, "failovers": 0, "errors": 0}

    @property
    def model(self) -> str:  # type: ignore[override]
        return getattr(self.primary, "model", "unknown")

    def is_configured(self) -> bool:
        return any(
            client is not None and client.is_configured()
            for client in (self.primary, self.fallback)
        )

    def active_providers(self) -> list[str]:
        return [
            client.provider
            for client in (self.primary, self.fallback)
            if client is not None and client.is_configured()
        ]

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.stats["calls"] += 1
        candidates = [c for c in (self.primary, self.fallback) if c is not None]
        if not candidates:
            raise LLMError("no LLM provider configured", provider="router", retryable=False)

        last_error: Exception | None = None
        for index, client in enumerate(candidates):
            if not client.is_configured():
                log.warning("llm.provider_unconfigured", provider=client.provider)
                continue
            if index > 0:
                self.stats["failovers"] += 1
                log.warning("llm.failover", to=client.provider, reason=str(last_error)[:200])
            for attempt in range(self.max_retries + 1):
                try:
                    response = await client.complete(
                        messages, tools, temperature=temperature, max_tokens=max_tokens
                    )
                    if index > 0:
                        log.info("llm.fallback_succeeded", provider=client.provider)
                    return response
                except LLMError as exc:
                    last_error = exc
                    self.stats["errors"] += 1
                    if not exc.retryable or attempt >= self.max_retries:
                        break
                    self.stats["retries"] += 1
                    delay = self.retry_base_delay * (2**attempt)
                    log.warning(
                        "llm.retry",
                        provider=client.provider,
                        attempt=attempt + 1,
                        error=str(exc)[:200],
                    )
                    await asyncio.sleep(delay)
                except Exception as exc:  # pragma: no cover - unexpected client bug
                    last_error = exc
                    self.stats["errors"] += 1
                    break

        raise LLMError(
            f"all LLM providers failed: {last_error}", provider="router", retryable=False
        )

    async def aclose(self) -> None:
        for client in (self.primary, self.fallback):
            if client is not None:
                await client.aclose()


_router: LLMRouter | None = None


def get_router() -> LLMRouter:
    """Process-wide router singleton."""
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router


async def close_router() -> None:
    global _router
    if _router is not None:
        await _router.aclose()
    _router = None


__all__ = ["LLMRouter", "build_client", "close_router", "get_router"]
