"""LLM router: retries, cross-provider failover and payload translation."""

from __future__ import annotations

import httpx
import pytest

from app.llm.base import LLMClient, LLMError, LLMResponse, Message, Usage
from app.llm.gemini_client import GeminiLLMClient, _clean_schema
from app.llm.groq_client import GroqLLMClient
from app.llm.mock import MockLLMClient
from app.llm.router import LLMRouter
from app.tools.definitions import TOOL_SPECS


class FlakyLLM(LLMClient):
    provider = "flaky"
    model = "flaky"

    def __init__(self, failures: int, retryable: bool = True) -> None:
        self.failures = failures
        self.retryable = retryable
        self.calls = 0

    async def complete(self, messages, tools=None, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        if self.calls <= self.failures:
            raise LLMError("boom", provider=self.provider, retryable=self.retryable)
        return LLMResponse(text="ok", usage=Usage(1, 1), provider=self.provider)


async def test_router_retries_the_primary_before_failing_over():
    primary = FlakyLLM(failures=1)
    fallback = MockLLMClient()
    router = LLMRouter(primary, fallback, max_retries=1, retry_base_delay=0)
    response = await router.complete([Message(role="user", content="hi")])
    assert response.provider == "flaky"
    assert router.stats["retries"] == 1
    assert router.stats["failovers"] == 0


async def test_router_fails_over_to_the_secondary_provider():
    primary = FlakyLLM(failures=5)
    fallback = MockLLMClient()
    router = LLMRouter(primary, fallback, max_retries=1, retry_base_delay=0)
    response = await router.complete([Message(role="user", content="hi")])
    assert response.provider == "mock"
    assert router.stats["failovers"] == 1


async def test_router_does_not_retry_non_retryable_errors():
    primary = FlakyLLM(failures=5, retryable=False)
    router = LLMRouter(primary, MockLLMClient(), max_retries=3, retry_base_delay=0)
    await router.complete([Message(role="user", content="hi")])
    assert primary.calls == 1


async def test_router_raises_when_every_provider_fails():
    router = LLMRouter(
        FlakyLLM(failures=9), FlakyLLM(failures=9), max_retries=0, retry_base_delay=0
    )
    with pytest.raises(LLMError):
        await router.complete([Message(role="user", content="hi")])


async def test_groq_client_parses_tool_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "llama-3.3-70b-versatile",
                "choices": [
                    {
                        "message": {
                            "content": "Sure.",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "confirm_person",
                                        "arguments": '{"is_correct_person": true}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 12},
            },
        )

    transport = httpx.MockTransport(handler)
    client = GroqLLMClient(api_key="test", client=httpx.AsyncClient(transport=transport))
    response = await client.complete([Message(role="user", content="hello")], [])
    assert response.tool_call.name == "confirm_person"
    assert response.tool_call.arguments == {"is_correct_person": True}
    assert response.usage.input_tokens == 100


async def test_groq_client_survives_unparseable_tool_arguments():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {"function": {"name": "confirm_person", "arguments": "{not json"}}
                            ],
                        }
                    }
                ]
            },
        )

    client = GroqLLMClient(
        api_key="test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    response = await client.complete([Message(role="user", content="hi")], [])
    assert "__unparseable__" in response.tool_call.arguments


async def test_groq_http_error_is_retryable_for_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    client = GroqLLMClient(
        api_key="test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(LLMError) as exc:
        await client.complete([Message(role="user", content="hi")])
    assert exc.value.retryable


async def test_gemini_client_translates_messages_and_tools():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Sure."},
                                {
                                    "functionCall": {
                                        "name": "confirm_person",
                                        "args": {"is_correct_person": True},
                                    }
                                },
                            ]
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 80, "candidatesTokenCount": 9},
            },
        )

    client = GeminiLLMClient(
        api_key="test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    tools = [TOOL_SPECS["confirm_person"].json_schema()]
    response = await client.complete(
        [Message(role="system", content="sys"), Message(role="user", content="hi")], tools
    )
    assert captured["systemInstruction"]["parts"][0]["text"] == "sys"
    assert captured["contents"][0]["role"] == "user"
    assert captured["tools"][0]["functionDeclarations"][0]["name"] == "confirm_person"
    assert response.tool_call.name == "confirm_person"
    assert response.usage.output_tokens == 9


def test_gemini_schema_cleaner_removes_unsupported_keywords():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "title": "Args",
        "properties": {
            "reason": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        },
    }
    cleaned = _clean_schema(schema)
    assert "additionalProperties" not in cleaned
    assert "title" not in cleaned
    assert cleaned["properties"]["reason"]["type"] == "string"


async def test_mock_client_is_deterministic():
    a, b = MockLLMClient(), MockLLMClient()
    messages = [
        Message(role="system", content="TOOL DISCIPLINE: STRICT\nCURRENT STATE: confirm_person"),
        Message(role="user", content="yes speaking"),
    ]
    first = await a.complete(messages)
    second = await b.complete(messages)
    assert first.text == second.text
    assert first.tool_call.name == second.tool_call.name == "confirm_person"


async def test_mock_client_can_inject_failures():
    client = MockLLMClient(fail_times=1)
    with pytest.raises(LLMError):
        await client.complete([Message(role="user", content="hi")])
    response = await client.complete([Message(role="user", content="hi")])
    assert response.provider == "mock"
