"""Google Gemini client (fallback LLM, Google AI Studio free tier).

Translates the provider-agnostic `Message`/tool-schema shapes into Gemini's
`generateContent` format and back, so the router can fail over transparently.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import get_settings
from app.llm.base import LLMClient, LLMError, LLMResponse, LLMTimeoutError, Message, ToolCall, Usage

_UNSUPPORTED_SCHEMA_KEYS = {
    "additionalProperties",
    "title",
    "default",
    "examples",
    "$schema",
    "exclusiveMinimum",
    "exclusiveMaximum",
}


def _clean_schema(node: Any) -> Any:
    """Strip JSON-Schema keywords the Gemini function-declaration parser rejects."""
    if isinstance(node, dict):
        cleaned: dict[str, Any] = {}
        for key, value in node.items():
            if key in _UNSUPPORTED_SCHEMA_KEYS:
                continue
            if key == "anyOf":
                # Gemini has no union type: collapse `T | null` to T (optional).
                non_null = [item for item in value if item.get("type") != "null"]
                if non_null:
                    collapsed = _clean_schema(non_null[0])
                    if isinstance(collapsed, dict):
                        cleaned.update(collapsed)
                continue
            cleaned[key] = _clean_schema(value)
        return cleaned
    if isinstance(node, list):
        return [_clean_schema(item) for item in node]
    return node


class GeminiLLMClient(LLMClient):
    provider = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.gemini_api_key
        self.model = model or settings.gemini_llm_model
        self.base_url = (base_url or settings.gemini_base_url).rstrip("/")
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

    # --- payload translation -------------------------------------------
    @staticmethod
    def _to_contents(messages: list[Message]) -> tuple[list[dict[str, Any]], str | None]:
        system_chunks: list[str] = []
        contents: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                system_chunks.append(message.content)
                continue
            role = "model" if message.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message.content}]})
        system = "\n\n".join(chunk for chunk in system_chunks if chunk) or None
        return contents, system

    @staticmethod
    def _to_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        declarations = []
        for tool in tools:
            function = tool.get("function", tool)
            declarations.append(
                {
                    "name": function["name"],
                    "description": function.get("description", ""),
                    "parameters": _clean_schema(function.get("parameters", {"type": "object"})),
                }
            )
        return [{"functionDeclarations": declarations}]

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        if not self.is_configured():
            raise LLMError("GEMINI_API_KEY is not set", provider=self.provider, retryable=False)

        settings = get_settings()
        contents, system = self._to_contents(messages)
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": settings.llm_temperature if temperature is None else temperature,
                "maxOutputTokens": settings.llm_max_tokens if max_tokens is None else max_tokens,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        gemini_tools = self._to_tools(tools)
        if gemini_tools:
            payload["tools"] = gemini_tools
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        started = time.perf_counter()
        url = f"{self.base_url}/models/{self.model}:generateContent"
        try:
            response = await self._http().post(
                url,
                headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
                json=payload,
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"gemini timeout after {self.timeout}s", provider=self.provider
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"gemini transport error: {exc}", provider=self.provider) from exc

        if response.status_code >= 400:
            retryable = response.status_code in {408, 429} or response.status_code >= 500
            raise LLMError(
                f"gemini http {response.status_code}: {response.text[:300]}",
                provider=self.provider,
                retryable=retryable,
            )

        body = response.json()
        candidates = body.get("candidates") or []
        parts = ((candidates[0].get("content") if candidates else {}) or {}).get("parts") or []

        text_chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        for part in parts:
            if "text" in part and part["text"]:
                text_chunks.append(part["text"])
            call = part.get("functionCall")
            if call:
                args = call.get("args") or {}
                if not isinstance(args, dict):
                    args = {"__unparseable__": str(args)[:200]}
                tool_calls.append(ToolCall(name=call.get("name", ""), arguments=args))

        usage_body = body.get("usageMetadata") or {}
        usage = Usage(
            input_tokens=int(usage_body.get("promptTokenCount") or 0),
            output_tokens=int(usage_body.get("candidatesTokenCount") or 0),
        )
        return LLMResponse(
            text="".join(text_chunks).strip(),
            tool_calls=tool_calls,
            usage=usage,
            provider=self.provider,
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            raw=body,
        )


__all__ = ["GeminiLLMClient"]
