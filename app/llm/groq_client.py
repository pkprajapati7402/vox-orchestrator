"""Groq chat-completions client (OpenAI-compatible API).

Primary reasoning/tool-calling provider: `llama-3.3-70b-versatile`.
Implemented directly on httpx to keep the dependency surface small and the
timeout/retry behaviour explicit.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.config import get_settings
from app.llm.base import LLMClient, LLMError, LLMResponse, LLMTimeoutError, Message, ToolCall, Usage
from app.logging_config import get_logger

log = get_logger(__name__)


class GroqLLMClient(LLMClient):
    provider = "groq"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.groq_api_key
        self.model = model or settings.groq_llm_model
        self.base_url = (base_url or settings.groq_base_url).rstrip("/")
        self.timeout = timeout or settings.llm_timeout_seconds
        self._client = client
        self._owns_client = client is None

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        if not self.is_configured():
            raise LLMError("GROQ_API_KEY is not set", provider=self.provider, retryable=False)

        settings = get_settings()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai() for m in messages],
            "temperature": settings.llm_temperature if temperature is None else temperature,
            "max_tokens": settings.llm_max_tokens if max_tokens is None else max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        started = time.perf_counter()
        try:
            response = await self._http().post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"groq timeout after {self.timeout}s", provider=self.provider
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"groq transport error: {exc}", provider=self.provider) from exc

        if response.status_code >= 400:
            retryable = response.status_code in {408, 409, 425, 429} or response.status_code >= 500
            raise LLMError(
                f"groq http {response.status_code}: {response.text[:300]}",
                provider=self.provider,
                retryable=retryable,
            )

        body = response.json()
        return _parse_openai_response(body, self.provider, self.model, started)


def _parse_openai_response(
    body: dict[str, Any], provider: str, model: str, started: float
) -> LLMResponse:
    choices = body.get("choices") or []
    message = (choices[0].get("message") if choices else {}) or {}
    text = (message.get("content") or "").strip()

    tool_calls: list[ToolCall] = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") or {}
        name = function.get("name") or ""
        arguments = function.get("arguments")
        parsed: dict[str, Any]
        if isinstance(arguments, dict):
            parsed = arguments
        else:
            try:
                parsed = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                # Malformed JSON is surfaced as an empty payload so schema
                # validation rejects it through the normal retry path.
                log.warning("llm.tool_arguments_unparseable", provider=provider, tool=name)
                parsed = {"__unparseable__": str(arguments)[:200]}
        if not isinstance(parsed, dict):
            parsed = {"__unparseable__": str(parsed)[:200]}
        tool_calls.append(ToolCall(name=name, arguments=parsed, call_id=raw.get("id")))

    usage_body = body.get("usage") or {}
    usage = Usage(
        input_tokens=int(usage_body.get("prompt_tokens") or 0),
        output_tokens=int(usage_body.get("completion_tokens") or 0),
    )
    return LLMResponse(
        text=text,
        tool_calls=tool_calls,
        usage=usage,
        provider=provider,
        model=body.get("model") or model,
        latency_ms=int((time.perf_counter() - started) * 1000),
        raw=body,
    )


__all__ = ["GroqLLMClient"]
