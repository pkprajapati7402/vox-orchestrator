"""The eval harness itself: personas, scoring rules and regression detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.enums import CallOutcome
from app.llm.mock import MockLLMClient
from app.pipeline.prompts import PROMPT_VARIANTS, PROMPT_VERSION
from eval.harness import run_persona, run_suite
from eval.personas import Persona, dump_personas, load_personas
from eval.report import render_comparison, render_report
from eval.scoring import aggregate, compare, is_subsequence, score_case
from eval.simulated_caller import ScriptedCaller


def test_catalog_size_and_coverage():
    personas = load_personas()
    assert 30 <= len(personas) <= 60, "spec calls for 30-50 synthetic personas"
    categories = {p.category for p in personas}
    assert {
        "price",
        "timing",
        "need_to_think",
        "not_interested",
        "interested",
        "rude",
    } <= categories
    assert len({p.id for p in personas}) == len(personas)


def test_personas_round_trip_through_json(tmp_path: Path):
    path = tmp_path / "personas.json"
    count = dump_personas(path)
    reloaded = load_personas(path)
    assert count == len(reloaded)
    assert isinstance(reloaded[0], Persona)
    assert json.loads(path.read_text())[0]["id"] == reloaded[0].id


def test_is_subsequence_semantics():
    assert is_subsequence(["a", "c"], ["a", "b", "c"])
    assert not is_subsequence(["c", "a"], ["a", "b", "c"])
    assert is_subsequence([], ["a"])


def test_scoring_flags_missing_and_forbidden_tools():
    persona = load_personas()[0]
    metrics = {
        "outcome": CallOutcome.NOT_INTERESTED.value,
        "final_state": "wrapup",
        "tool_sequence": ["confirm_person", "schedule_meeting"],
        "turns": 4,
        "hallucinated_tool_calls": 1,
        "rejected_tool_calls": [{"tool": "teleport"}],
        "cost": {"cost_usd": 0.01},
    }
    result = score_case(persona, metrics)
    assert not result.task_completed
    assert not result.correct_tool_sequence
    assert result.hallucinated_tool_call
    assert not result.passed
    assert any("missing required tool" in note for note in result.failure_notes)


async def test_interested_persona_books_a_meeting():
    persona = next(p for p in load_personas() if p.id == "interested_01")
    result = await run_persona(persona, agent_llm=MockLLMClient())
    assert result.actual_outcome == CallOutcome.MEETING_BOOKED.value
    assert result.passed
    assert "schedule_meeting" in result.tool_sequence


async def test_price_objection_persona_is_classified_before_closing():
    persona = next(p for p in load_personas() if p.id == "price_01")
    result = await run_persona(persona, agent_llm=MockLLMClient())
    assert "classify_objection" in result.tool_sequence
    assert result.tool_sequence.index("classify_objection") < result.tool_sequence.index(
        "schedule_meeting"
    )


async def test_voicemail_persona_never_pitches():
    persona = next(p for p in load_personas() if p.id == "voicemail_01")
    result = await run_persona(persona, agent_llm=MockLLMClient())
    assert result.actual_outcome == CallOutcome.VOICEMAIL.value
    assert result.tool_sequence == ["log_voicemail"]


async def test_abrupt_hangup_persona_is_recorded_as_hung_up():
    persona = next(p for p in load_personas() if p.id == "rude_01")
    result = await run_persona(persona, agent_llm=MockLLMClient())
    assert result.actual_outcome == CallOutcome.HUNG_UP.value


async def test_silent_persona_ends_as_no_answer():
    persona = next(p for p in load_personas() if p.id == "silence_01")
    result = await run_persona(persona, agent_llm=MockLLMClient())
    assert result.actual_outcome == CallOutcome.NO_ANSWER.value


async def test_full_suite_meets_the_baseline_thresholds():
    report = await run_suite(label="test-baseline")
    score = report.score
    assert score.personas_run >= 30
    assert score.task_completion_rate >= 0.9
    assert score.correct_tool_sequence_rate >= 0.9
    assert score.hallucinated_tool_call_rate == 0.0
    assert 0 < score.avg_turns_to_resolution < 15
    markdown = render_report(report)
    assert "Task completion rate" in markdown
    assert "By persona category" in markdown


async def test_prompt_regression_is_detected():
    """Phase 6 exit criterion: a deliberate prompt change must move the score."""
    baseline = await run_suite(label="strict", prompt_variant=PROMPT_VERSION)
    candidate = await run_suite(label="loose", prompt_variant="v1-loose")

    assert candidate.score.correct_tool_sequence_rate < baseline.score.correct_tool_sequence_rate
    diff = compare(baseline.score.as_dict(), candidate.score.as_dict())
    assert diff["regressed"] is True
    markdown = render_comparison(baseline.as_dict(), candidate.as_dict())
    assert "Regression detected: YES" in markdown


def test_prompt_variants_are_distinct():
    strict = PROMPT_VARIANTS[PROMPT_VERSION].rules
    loose = PROMPT_VARIANTS["v1-loose"].rules
    assert "TOOL DISCIPLINE: STRICT" in strict
    assert "TOOL DISCIPLINE: LOOSE" in loose


async def test_scripted_caller_repeats_itself_when_asked():
    persona = next(p for p in load_personas() if p.id == "noisy_01")
    caller = ScriptedCaller(persona)
    from app.enums import ConversationState

    first = await caller.reply(
        "Hi, am I speaking with the owner?", ConversationState.CONFIRM_PERSON
    )
    assert first.confidence == persona.behaviour.asr_confidence
    repeat = await caller.reply(
        "Sorry, the line broke up a little — could you say that once more?",
        ConversationState.CONFIRM_PERSON,
    )
    assert repeat.text == first.text
    assert repeat.confidence == 0.95


def test_aggregate_handles_an_empty_run():
    score = aggregate([])
    assert score.personas_run == 0
    assert score.task_completion_rate == 0


@pytest.mark.usefixtures("db")
async def test_eval_results_can_be_persisted():
    from app.db.base import session_scope
    from app.db.repositories import EvalRepository
    from eval.run_eval import persist_report

    report = await run_suite(load_personas()[:3], label="persist-test")
    await persist_report(report)
    async with session_scope() as session:
        runs = await EvalRepository(session).latest_runs()
        assert runs[0].label == "persist-test"
        assert runs[0].personas_run == 3
        assert len(runs[0].results) == 3
