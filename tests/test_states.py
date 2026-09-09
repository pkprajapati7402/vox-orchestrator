"""State machine invariants (Project-Details.md §4)."""

from __future__ import annotations

from app.enums import CallOutcome, ConversationState
from app.pipeline.flow import build_flow_config, flow_summary
from app.pipeline.states import TRANSITIONS, can_transition, default_outcome, validate_machine
from app.tools.definitions import TOOL_SPECS


def test_every_state_is_reachable_from_greeting():
    validate_machine()


def test_transitions_match_the_state_diagram():
    assert TRANSITIONS[ConversationState.GREETING] >= {ConversationState.CONFIRM_PERSON}
    assert TRANSITIONS[ConversationState.CONFIRM_PERSON] == {
        ConversationState.DISCOVERY,
        ConversationState.END_CALL,
    }
    assert TRANSITIONS[ConversationState.DISCOVERY] >= {ConversationState.PITCH}
    assert TRANSITIONS[ConversationState.PITCH] >= {
        ConversationState.OBJECTION_HANDLING,
        ConversationState.CLOSE,
    }
    assert TRANSITIONS[ConversationState.OBJECTION_HANDLING] >= {
        ConversationState.PITCH,
        ConversationState.CLOSE,
        ConversationState.END_CALL,
    }
    assert TRANSITIONS[ConversationState.CLOSE] >= {
        ConversationState.BOOK_MEETING,
        ConversationState.END_CALL,
    }
    assert TRANSITIONS[ConversationState.BOOK_MEETING] >= {ConversationState.WRAPUP}
    assert TRANSITIONS[ConversationState.END_CALL] == {ConversationState.WRAPUP}
    assert TRANSITIONS[ConversationState.WRAPUP] == frozenset()


def test_illegal_transitions_are_rejected():
    assert not can_transition(ConversationState.GREETING, ConversationState.BOOK_MEETING)
    assert not can_transition(ConversationState.WRAPUP, ConversationState.PITCH)
    assert can_transition(ConversationState.PITCH, ConversationState.CLOSE)
    # Self-loops only make sense while the agent is still selling.
    assert can_transition(
        ConversationState.OBJECTION_HANDLING, ConversationState.OBJECTION_HANDLING
    )
    assert not can_transition(ConversationState.WRAPUP, ConversationState.WRAPUP)


def test_every_tool_target_state_is_a_legal_edge():
    """The registry and the state table must agree."""
    from app.tools.schemas import (
        ConfirmPersonArgs,
        MarkNotInterestedArgs,
        ResolveObjectionArgs,
    )

    samples = {
        ConfirmPersonArgs: [
            ConfirmPersonArgs(is_correct_person=True),
            ConfirmPersonArgs(is_correct_person=False, reason="wrong number"),
        ],
        ResolveObjectionArgs: [
            ResolveObjectionArgs(objection_type="price", resolved=True),
            ResolveObjectionArgs(objection_type="price", resolved=False),
        ],
        MarkNotInterestedArgs: [MarkNotInterestedArgs(reason="not interested")],
    }
    for spec in TOOL_SPECS.values():
        candidates = samples.get(spec.args_model)
        if candidates is None:
            continue
        for args in candidates:
            for state in spec.allowed_states:
                target = spec.transition(args)
                assert can_transition(state, target), f"{spec.name}: {state} -> {target}"


def test_default_outcomes_are_sane():
    assert default_outcome(ConversationState.BOOK_MEETING) is CallOutcome.MEETING_BOOKED
    assert default_outcome(ConversationState.CONFIRM_PERSON) is CallOutcome.WRONG_PERSON


def test_flow_config_covers_every_state():
    from app.pipeline.engine import LeadContext

    config = build_flow_config(LeadContext(business_name="Test Cafe"))
    assert config["initial_node"] == ConversationState.GREETING.value
    assert set(config["nodes"]) == {state.value for state in ConversationState}
    greeting = config["nodes"][ConversationState.GREETING.value]
    assert greeting["role_messages"][0]["content"].startswith("You are")
    assert config["nodes"][ConversationState.WRAPUP.value]["post_actions"]


def test_flow_summary_lists_tools_per_state():
    summary = flow_summary()
    assert "confirm_person" in summary[ConversationState.CONFIRM_PERSON.value]
    assert "schedule_meeting" in summary[ConversationState.CLOSE.value]
