"""Conversation engine: happy paths, every branch of the diagram, and guards."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.enums import CallOutcome, ConversationState
from app.llm.base import LLMClient, LLMError, LLMResponse, ToolCall, Usage
from app.llm.mock import MockLLMClient
from app.pipeline.engine import ConversationEngine
from app.pipeline.persistence import MemoryTurnSink


class ScriptedLLM(LLMClient):
    """Replays a fixed list of (text, tool_call) responses."""

    provider = "scripted"
    model = "scripted"

    def __init__(self, script: list[tuple[str, ToolCall | None]]) -> None:
        self.script = list(script)
        self.seen: list[list] = []

    async def complete(self, messages, tools=None, **kwargs):  # noqa: ANN001, ANN003
        self.seen.append(messages)
        text, call = self.script.pop(0) if self.script else ("Okay.", None)
        return LLMResponse(
            text=text,
            tool_calls=[call] if call else [],
            usage=Usage(100, 20),
            provider=self.provider,
            model=self.model,
        )


async def run(engine: ConversationEngine, utterances: list[str]) -> list:
    turns = [await engine.start()]
    for utterance in utterances:
        turns.append(await engine.handle_user(utterance))
        if engine.ended:
            break
    return turns


async def test_greeting_opens_with_lead_context(lead_context, mock_llm):
    engine = ConversationEngine(lead_context, mock_llm)
    turn = await engine.start()
    assert "Glow Studio Salon" in turn.text
    assert "Hauz Khas" in turn.text
    assert engine.state is ConversationState.CONFIRM_PERSON


async def test_happy_path_books_a_meeting(lead_context, mock_llm):
    engine = ConversationEngine(lead_context, mock_llm)
    await run(
        engine,
        [
            "Yes, speaking.",
            "Mostly walk-ins and referrals.",
            "Okay, that sounds interesting. How does that work?",
            "Thursday at 4 works",
        ],
    )
    assert engine.ended
    assert engine.outcome is CallOutcome.MEETING_BOOKED
    assert engine.state is ConversationState.WRAPUP
    assert engine.tool_sequence == [
        "confirm_person",
        "capture_discovery",
        "move_to_close",
        "schedule_meeting",
    ]
    assert engine.meeting_at is not None


async def test_wrong_person_ends_the_call(lead_context, mock_llm):
    engine = ConversationEngine(lead_context, mock_llm)
    await run(engine, ["He's not here right now, I just work here."])
    assert engine.ended
    assert engine.outcome is CallOutcome.WRONG_PERSON
    assert engine.tool_sequence == ["confirm_person"]


async def test_objection_is_classified_before_closing(lead_context, mock_llm):
    engine = ConversationEngine(lead_context, mock_llm)
    await run(
        engine,
        [
            "Yes, speaking.",
            "Walk-ins mostly.",
            "Honestly that sounds expensive for us.",
            "Okay, that makes sense.",
            "Thursday at 4 works",
        ],
    )
    assert "classify_objection" in engine.tool_sequence
    assert engine.tool_sequence.index("classify_objection") < len(engine.tool_sequence) - 1
    assert engine.objection_guard.types_seen == ["price"]


async def test_hard_no_marks_not_interested(lead_context, mock_llm):
    engine = ConversationEngine(lead_context, mock_llm)
    await run(
        engine,
        [
            "Yes, speaking.",
            "Referrals.",
            "We already have someone handling this, not interested.",
            "No thanks, please don't call again.",
        ],
    )
    assert engine.outcome is CallOutcome.NOT_INTERESTED
    assert "mark_not_interested" in engine.tool_sequence
    assert "schedule_meeting" not in engine.tool_sequence


async def test_voicemail_skips_the_conversation(lead_context, mock_llm):
    engine = ConversationEngine(lead_context, mock_llm)
    await engine.start()
    turn = await engine.handle_voicemail("machine_end_beep")
    assert turn.ended
    assert engine.outcome is CallOutcome.VOICEMAIL
    assert engine.tool_sequence == ["log_voicemail"]


async def test_silence_prompts_once_then_hangs_up(lead_context, mock_llm):
    engine = ConversationEngine(lead_context, mock_llm)
    await engine.start()
    first = await engine.handle_silence()
    assert "still there" in first.text.lower()
    assert not first.ended
    second = await engine.handle_silence()
    assert second.ended
    assert engine.outcome is CallOutcome.NO_ANSWER


async def test_low_asr_confidence_asks_for_a_repeat_once(lead_context, mock_llm):
    engine = ConversationEngine(lead_context, mock_llm)
    await engine.start()
    turn = await engine.handle_user("mumble mumble", confidence=0.2)
    assert "once more" in turn.text.lower()
    assert engine.state is ConversationState.CONFIRM_PERSON
    assert engine.asr_gate.repeats_used == 1
    # The second low-confidence utterance is processed rather than looping.
    turn = await engine.handle_user("yes speaking", confidence=0.2)
    assert turn.tool_name == "confirm_person"


async def test_hallucinated_tool_call_is_retried_then_falls_back(lead_context):
    llm = MockLLMClient(mode="hallucinating")
    engine = ConversationEngine(lead_context, llm)
    await engine.start()
    turn = await engine.handle_user("Yes, speaking.")
    # The illegal schedule_meeting is rejected; the retry produces a valid call.
    assert engine.hallucinated_tool_calls == 1
    assert turn.tool_name == "confirm_person"
    assert engine.rejected_tool_calls[0]["kind"] == "illegal_state"


async def test_malformed_arguments_are_retried(lead_context):
    llm = MockLLMClient(mode="malformed")
    engine = ConversationEngine(lead_context, llm)
    await engine.start()
    turn = await engine.handle_user("Yes, speaking.")
    assert engine.invalid_argument_calls == 1
    assert engine.hallucinated_tool_calls == 0
    assert turn.tool_name == "confirm_person"


async def test_unfixable_tool_call_falls_back_to_a_scripted_line(lead_context):
    bad_call = ToolCall("teleport", {"to": "mars"})
    llm = ScriptedLLM([("Sure thing.", bad_call), ("Sure thing.", bad_call)])
    engine = ConversationEngine(lead_context, llm)
    await engine.start()
    turn = await engine.handle_user("Yes, speaking.")
    assert turn.used_fallback_line
    assert turn.invocation is None
    assert engine.state is ConversationState.CONFIRM_PERSON  # no silent transition
    assert engine.fallback_lines_used == 1


async def test_objection_loop_guard_forces_close_then_end(lead_context):
    """More than MAX_OBJECTION_CYCLES objections must force Close, then EndCall."""
    settings = get_settings()
    call = ToolCall("classify_objection", {"type": "price"})
    llm = ScriptedLLM([("I hear you on the cost.", call)] * 10)
    engine = ConversationEngine(lead_context, llm)
    await engine.start()
    engine.state = ConversationState.PITCH  # jump past the opening states

    states = []
    for _ in range(8):
        if engine.ended:
            break
        turn = await engine.handle_user("That is too expensive.")
        states.append(turn.state_after)

    assert engine.objection_guard.cycles > settings.max_objection_cycles
    assert ConversationState.CLOSE in states, "the guard must force a Close attempt"
    assert engine.ended and engine.state is ConversationState.WRAPUP


async def test_llm_outage_produces_a_safe_line_not_a_crash(lead_context):
    class DeadLLM(LLMClient):
        provider = "dead"
        model = "dead"

        async def complete(self, messages, tools=None, **kwargs):  # noqa: ANN001, ANN003
            raise LLMError("provider down", provider="dead")

    engine = ConversationEngine(lead_context, DeadLLM())
    await engine.start()
    turn = await engine.handle_user("Yes, speaking.")
    assert turn.used_fallback_line
    assert not engine.ended
    assert engine.llm_errors == 1


async def test_turn_budget_terminates_the_call(lead_context, mock_llm):
    engine = ConversationEngine(lead_context, mock_llm)
    engine.turn_budget.max_turns = 3
    await engine.start()
    for _ in range(5):
        if engine.ended:
            break
        await engine.handle_user("Hmm.")
    assert engine.ended
    assert engine.outcome in {CallOutcome.FAILED, CallOutcome.NOT_INTERESTED}


async def test_transcript_and_cost_are_recorded(lead_context, mock_llm):
    sink = MemoryTurnSink()
    engine = ConversationEngine(lead_context, mock_llm, sink=sink)
    await run(engine, ["Yes, speaking.", "Walk-ins.", "Tell me more", "Thursday at 4 works"])
    assert len(sink.records) == len(engine.transcript) > 4
    metrics = engine.metrics()
    assert metrics["cost"]["llm_input_tokens"] > 0
    assert metrics["cost"]["tts_characters"] > 0
    assert metrics["cost"]["cost_usd"] >= 0
    tool_turns = [r for r in engine.transcript if r.tool_called]
    assert all(turn.tool_valid for turn in tool_turns)


async def test_state_prompt_is_injected_every_turn(lead_context):
    llm = ScriptedLLM([("Hi.", None)])
    engine = ConversationEngine(lead_context, llm)
    await engine.start()
    await engine.handle_user("Hello?")
    system_messages = [m.content for m in llm.seen[0] if m.role == "system"]
    assert any("CURRENT STATE: confirm_person" in content for content in system_messages)
    assert any("TOOL DISCIPLINE: STRICT" in content for content in system_messages)


async def test_engine_is_idempotent_after_the_call_ends(lead_context, mock_llm):
    engine = ConversationEngine(lead_context, mock_llm)
    await engine.start()
    await engine.handle_voicemail()
    turn = await engine.handle_user("hello?")
    assert turn.text == "" and turn.ended


@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("I think you have the wrong number.", CallOutcome.WRONG_PERSON),
        ("Sir is unavailable right now, call back later.", CallOutcome.UNAVAILABLE),
    ],
)
async def test_confirm_person_outcomes(lead_context, mock_llm, utterance, expected):
    engine = ConversationEngine(lead_context, mock_llm)
    await run(engine, [utterance])
    assert engine.outcome is expected
