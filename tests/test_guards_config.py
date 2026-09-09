"""Guards, settings and the session store."""

from __future__ import annotations

import pytest

from app.cache.session import MemorySessionStore, get_session_store, session_key
from app.config import Settings
from app.enums import CallOutcome, ConversationState
from app.pipeline.guards import (
    ASRConfidenceGate,
    ObjectionLoopGuard,
    SilenceGuard,
    ToolRetryBudget,
    TurnBudget,
)


def test_asr_gate_asks_for_one_repeat_only():
    gate = ASRConfidenceGate(min_confidence=0.6, max_repeats=1)
    assert gate.should_ask_repeat(0.2)
    gate.register_repeat()
    assert not gate.should_ask_repeat(0.2)
    assert not gate.should_ask_repeat(0.9)
    assert not gate.should_ask_repeat(None)


def test_silence_guard_prompts_then_hangs_up():
    guard = SilenceGuard(max_prompts=1)
    assert guard.next_action() == "prompt"
    assert guard.next_action() == "hangup"
    guard.reset()
    assert guard.next_action() == "prompt"


def test_objection_loop_guard_escalates():
    guard = ObjectionLoopGuard(max_cycles=3)
    for _ in range(3):
        guard.register("price")
        assert guard.forced_state() is None
    guard.register("timing")
    assert guard.forced_state() is ConversationState.CLOSE
    guard.register("timing")
    assert guard.forced_state() is ConversationState.END_CALL
    assert guard.types_seen.count("price") == 3


def test_turn_budget_and_retry_budget():
    budget = TurnBudget(max_turns=2)
    budget.register()
    assert not budget.exhausted
    budget.register()
    assert budget.exhausted

    retries = ToolRetryBudget(max_retries=1)
    assert retries.can_retry()
    retries.consume()
    assert not retries.can_retry()
    retries.reset()
    assert retries.can_retry()


def test_settings_parse_the_backoff_schedule():
    settings = Settings(call_retry_backoff_minutes="15, 45,120")
    assert settings.retry_backoff_minutes == [15, 45, 120]
    assert Settings(call_retry_backoff_minutes="nonsense").retry_backoff_minutes == [60]


def test_settings_derive_the_websocket_url():
    assert (
        Settings(public_base_url="https://x.ngrok.app/").websocket_base_url == "wss://x.ngrok.app"
    )
    assert (
        Settings(public_base_url="http://localhost:8000").websocket_base_url
        == "ws://localhost:8000"
    )


def test_settings_flags():
    settings = Settings(database_url="sqlite+aiosqlite:///./x.sqlite3")
    assert settings.is_sqlite
    assert not settings.twilio_configured()
    assert not settings.llm_configured()
    assert Settings(groq_api_key="x").llm_configured()


def test_outcome_retry_semantics():
    assert CallOutcome.NO_ANSWER.is_retryable
    assert CallOutcome.VOICEMAIL.is_retryable
    assert not CallOutcome.MEETING_BOOKED.is_retryable
    assert not CallOutcome.NOT_INTERESTED.is_retryable
    assert CallOutcome.MEETING_BOOKED.is_success


async def test_memory_session_store_round_trip():
    store = MemorySessionStore()
    key = session_key("call-1")
    assert await store.get(key) is None
    await store.set(key, {"state": "pitch"})
    assert (await store.get(key))["state"] == "pitch"
    await store.update(key, {"turns": 3})
    value = await store.get(key)
    assert value == {"state": "pitch", "turns": 3}
    await store.delete(key)
    assert await store.get(key) is None


async def test_session_store_falls_back_to_memory_without_redis():
    store = get_session_store()
    assert isinstance(store, MemorySessionStore)
    assert await store.ping()


@pytest.mark.parametrize(
    "state,expected",
    [
        (ConversationState.WRAPUP, True),
        (ConversationState.PITCH, False),
    ],
)
def test_terminal_state_flag(state, expected):
    assert state.is_terminal is expected
