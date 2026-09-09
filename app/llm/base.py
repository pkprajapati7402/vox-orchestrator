"""LLM abstraction: provider-agnostic message/tool-call types and the client protocol."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


class LLMError(RuntimeError):
    """Any provider-side failure (HTTP, auth, rate limit, malformed body)."""

    def __init__(self, message: str, *, provider: str = "", retryable: bool = True) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class LLMTimeoutError(LLMError):
    """The provider did not answer inside the configured budget."""


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None

    def to_openai(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            payload["name"] = self.name
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        return payload


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True)
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    raw: dict[str, Any] | None = None

    @property
    def tool_call(self) -> ToolCall | None:
        """The single tool call the state machine acts on (first one wins)."""
        return self.tool_calls[0] if self.tool_calls else None


class LLMClient(abc.ABC):
    """Minimal async chat-completion interface with tool calling."""

    provider: str = "unknown"
    model: str = ""

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Return the assistant's reply, including any tool calls."""

    async def aclose(self) -> None:  # pragma: no cover - overridden where needed
        return None

    def is_configured(self) -> bool:  # pragma: no cover - overridden
        return True


__all__ = [
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "LLMTimeoutError",
    "Message",
    "Role",
    "ToolCall",
    "Usage",
]
