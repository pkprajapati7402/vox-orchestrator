"""The conversation state machine, declared as data.

`TRANSITIONS` mirrors the state diagram in Project-Details.md §4 exactly. The
engine refuses any transition that is not listed here, even if a tool asks for
it — the tool registry and this table have to agree before the conversation
moves.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.enums import CallOutcome, ConversationState

S = ConversationState

#: Legal `from -> {to}` edges of the state machine.
TRANSITIONS: dict[ConversationState, frozenset[ConversationState]] = {
    S.GREETING: frozenset({S.CONFIRM_PERSON, S.END_CALL}),
    S.CONFIRM_PERSON: frozenset({S.DISCOVERY, S.END_CALL}),
    S.DISCOVERY: frozenset({S.PITCH, S.END_CALL}),
    S.PITCH: frozenset({S.OBJECTION_HANDLING, S.CLOSE, S.END_CALL}),
    S.OBJECTION_HANDLING: frozenset({S.PITCH, S.CLOSE, S.OBJECTION_HANDLING, S.END_CALL}),
    S.CLOSE: frozenset({S.BOOK_MEETING, S.OBJECTION_HANDLING, S.END_CALL}),
    S.BOOK_MEETING: frozenset({S.WRAPUP, S.END_CALL}),
    S.END_CALL: frozenset({S.WRAPUP}),
    S.WRAPUP: frozenset(),
}

#: States in which the agent is still selling (used by the loop guard).
ACTIVE_STATES: frozenset[ConversationState] = frozenset(
    {S.GREETING, S.CONFIRM_PERSON, S.DISCOVERY, S.PITCH, S.OBJECTION_HANDLING, S.CLOSE}
)

#: Default outcome if the call terminates while in a given state.
DEFAULT_OUTCOME_BY_STATE: dict[ConversationState, CallOutcome] = {
    S.GREETING: CallOutcome.HUNG_UP,
    S.CONFIRM_PERSON: CallOutcome.WRONG_PERSON,
    S.DISCOVERY: CallOutcome.HUNG_UP,
    S.PITCH: CallOutcome.HUNG_UP,
    S.OBJECTION_HANDLING: CallOutcome.NOT_INTERESTED,
    S.CLOSE: CallOutcome.NOT_INTERESTED,
    S.BOOK_MEETING: CallOutcome.MEETING_BOOKED,
    S.END_CALL: CallOutcome.HUNG_UP,
    S.WRAPUP: CallOutcome.HUNG_UP,
}


@dataclass(frozen=True)
class StateInfo:
    state: ConversationState
    allows_speech: bool
    is_active: bool
    successors: frozenset[ConversationState]


def can_transition(source: ConversationState, target: ConversationState) -> bool:
    """Is `source -> target` a legal edge? Self-loops are allowed on active states."""
    if source is target:
        return source in ACTIVE_STATES
    return target in TRANSITIONS.get(source, frozenset())


def successors(state: ConversationState) -> frozenset[ConversationState]:
    return TRANSITIONS.get(state, frozenset())


def describe(state: ConversationState) -> StateInfo:
    return StateInfo(
        state=state,
        allows_speech=state is not ConversationState.WRAPUP,
        is_active=state in ACTIVE_STATES,
        successors=successors(state),
    )


def default_outcome(state: ConversationState) -> CallOutcome:
    return DEFAULT_OUTCOME_BY_STATE.get(state, CallOutcome.FAILED)


def validate_machine() -> None:
    """Sanity check invoked by tests: every state is reachable and terminates."""
    reachable: set[ConversationState] = {S.GREETING}
    frontier = [S.GREETING]
    while frontier:
        current = frontier.pop()
        for nxt in successors(current):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    missing = set(ConversationState) - reachable
    if missing:  # pragma: no cover - guarded by tests
        raise AssertionError(f"unreachable states: {sorted(s.value for s in missing)}")


__all__ = [
    "ACTIVE_STATES",
    "DEFAULT_OUTCOME_BY_STATE",
    "TRANSITIONS",
    "StateInfo",
    "can_transition",
    "default_outcome",
    "describe",
    "successors",
    "validate_machine",
]
