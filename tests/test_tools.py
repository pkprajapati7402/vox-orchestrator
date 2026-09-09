"""Tool registry: schema validation, state legality and transition mapping."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.enums import CallOutcome, ConversationState, ObjectionType
from app.tools.definitions import TOOL_SPECS, tool_names_for_state, tool_schemas_for_state
from app.tools.registry import ToolValidationError, execute_tool, validate_tool_call
from app.tools.schemas import ScheduleMeetingArgs


def test_every_spec_exposes_a_json_schema():
    for name, spec in TOOL_SPECS.items():
        schema = spec.json_schema()
        assert schema["function"]["name"] == name
        assert schema["function"]["parameters"]["type"] == "object"
        assert "$defs" not in schema["function"]["parameters"], "enum refs must be inlined"


def test_spec_tools_from_project_details_all_exist():
    required = {
        "confirm_person",
        "classify_objection",
        "schedule_meeting",
        "mark_not_interested",
        "end_call",
        "log_voicemail",
    }
    assert required <= set(TOOL_SPECS)


def test_confirm_person_transitions():
    ok = execute_tool(
        "confirm_person", {"is_correct_person": True}, ConversationState.CONFIRM_PERSON
    )
    assert ok.to_state is ConversationState.DISCOVERY
    assert ok.outcome is None

    nope = execute_tool(
        "confirm_person",
        {"is_correct_person": False, "reason": "wrong number"},
        ConversationState.CONFIRM_PERSON,
    )
    assert nope.to_state is ConversationState.END_CALL
    assert nope.outcome is CallOutcome.WRONG_PERSON

    away = execute_tool(
        "confirm_person",
        {"is_correct_person": False, "reason": "owner is unavailable, call later"},
        ConversationState.CONFIRM_PERSON,
    )
    assert away.outcome is CallOutcome.UNAVAILABLE


def test_unknown_tool_is_rejected_as_hallucination():
    with pytest.raises(ToolValidationError) as exc:
        validate_tool_call("book_flight", {}, ConversationState.PITCH)
    assert exc.value.kind == "unknown_tool"
    assert exc.value.is_hallucination


def test_tool_called_from_illegal_state_is_rejected():
    with pytest.raises(ToolValidationError) as exc:
        validate_tool_call(
            "schedule_meeting",
            {"date": "2099-01-01", "time": "11:00", "contact_confirmation": "Ritu, this number"},
            ConversationState.GREETING,
        )
    assert exc.value.kind == "illegal_state"
    assert exc.value.is_hallucination


def test_invalid_arguments_are_rejected_but_not_hallucinations():
    with pytest.raises(ToolValidationError) as exc:
        validate_tool_call("classify_objection", {"type": "vibes"}, ConversationState.PITCH)
    assert exc.value.kind == "invalid_args"
    assert not exc.value.is_hallucination


def test_extra_arguments_are_forbidden():
    with pytest.raises(ToolValidationError):
        validate_tool_call(
            "confirm_person",
            {"is_correct_person": True, "sneaky": "value"},
            ConversationState.CONFIRM_PERSON,
        )


def test_schedule_meeting_rejects_past_dates_and_bad_times():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with pytest.raises(ToolValidationError):
        validate_tool_call(
            "schedule_meeting",
            {"date": yesterday, "time": "11:00", "contact_confirmation": "Ritu"},
            ConversationState.CLOSE,
        )
    with pytest.raises(ToolValidationError):
        validate_tool_call(
            "schedule_meeting",
            {
                "date": (date.today() + timedelta(days=1)).isoformat(),
                "time": "25:00",
                "contact_confirmation": "Ritu",
            },
            ConversationState.CLOSE,
        )


def test_schedule_meeting_produces_meeting_metadata():
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    invocation = execute_tool(
        "schedule_meeting",
        {"date": tomorrow, "time": "16:00", "contact_confirmation": "Ritu, +911140001001"},
        ConversationState.CLOSE,
    )
    assert invocation.to_state is ConversationState.BOOK_MEETING
    assert invocation.outcome is CallOutcome.MEETING_BOOKED
    assert invocation.metadata["meeting_at"].tzinfo is not None
    assert invocation.metadata["meeting_contact"].startswith("Ritu")


def test_end_call_requires_a_terminal_outcome():
    with pytest.raises(ToolValidationError):
        validate_tool_call(
            "end_call", {"outcome": "in_progress", "reason": "nope"}, ConversationState.PITCH
        )
    invocation = execute_tool(
        "end_call", {"outcome": "hung_up", "reason": "lead hung up"}, ConversationState.PITCH
    )
    assert invocation.terminal and invocation.outcome is CallOutcome.HUNG_UP


def test_classify_objection_metadata_and_state():
    invocation = execute_tool("classify_objection", {"type": "price"}, ConversationState.PITCH)
    assert invocation.to_state is ConversationState.OBJECTION_HANDLING
    assert invocation.metadata["objection_type"] == ObjectionType.PRICE.value


def test_tools_available_per_state_match_the_design():
    assert tool_names_for_state(ConversationState.CONFIRM_PERSON) == {
        "confirm_person",
        "end_call",
        "log_voicemail",
    }
    assert "schedule_meeting" in tool_names_for_state(ConversationState.CLOSE)
    assert "schedule_meeting" not in tool_names_for_state(ConversationState.PITCH)
    assert tool_schemas_for_state(ConversationState.DISCOVERY)


def test_schedule_meeting_converts_ist_to_utc():
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    args = ScheduleMeetingArgs(date=tomorrow, time="16:00", contact_confirmation="Ritu here")
    assert args.to_datetime().hour == 10  # 16:00 IST -> 10:30 UTC
    assert args.to_datetime().minute == 30
