"""Tool specifications: which tool is callable from which state, what it does,
and the OpenAI/Groq-compatible JSON schema handed to the LLM.

The six tools in Project-Details.md §5 cover most arrows of the state diagram.
Four transition tools are added so that *every* arrow in §4 is driven by a
validated tool call rather than by the model free-forming its way forward:

    capture_discovery   Discovery          -> Pitch
    resolve_objection   ObjectionHandling  -> Pitch (or stay)
    move_to_close       Pitch/Objection    -> Close
    (schedule_meeting)  Close              -> BookMeeting   [already in the spec]

Greeting -> ConfirmPerson is the agent's own opening turn and is performed by
the engine immediately after the greeting line is spoken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.enums import CallOutcome, ConversationState
from app.tools.schemas import (
    CaptureDiscoveryArgs,
    ClassifyObjectionArgs,
    ConfirmPersonArgs,
    EndCallArgs,
    LogVoicemailArgs,
    MarkNotInterestedArgs,
    MoveToCloseArgs,
    ResolveObjectionArgs,
    ScheduleMeetingArgs,
    ToolArgs,
)

ANY_STATE: frozenset[ConversationState] = frozenset(
    {
        ConversationState.GREETING,
        ConversationState.CONFIRM_PERSON,
        ConversationState.DISCOVERY,
        ConversationState.PITCH,
        ConversationState.OBJECTION_HANDLING,
        ConversationState.CLOSE,
        ConversationState.BOOK_MEETING,
    }
)


@dataclass(frozen=True)
class ToolSpec:
    """Everything the runtime needs to expose, validate and route one tool."""

    name: str
    description: str
    args_model: type[ToolArgs]
    allowed_states: frozenset[ConversationState]
    #: Resolves the next state from the validated arguments.
    transition: Any
    #: Outcome implied by the tool, if it terminates the call.
    outcome: Any = None
    terminal: bool = False
    examples: tuple[str, ...] = field(default_factory=tuple)

    def json_schema(self) -> dict[str, Any]:
        """OpenAI/Groq `tools` entry (Gemini uses the same shape, unwrapped)."""
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        # Providers with limited JSON-Schema support choke on draft-07 leftovers.
        schema.pop("definitions", None)
        schema.setdefault("type", "object")
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _inline_enum_refs(schema),
            },
        }


def _inline_enum_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline `$ref`/`$defs` enums so providers with limited schema support work."""
    defs = schema.pop("$defs", {})
    if not defs:
        return schema

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node.pop("$ref")
                name = ref.rsplit("/", 1)[-1]
                target = dict(defs.get(name, {}))
                target.pop("title", None)
                merged = {**target, **node}
                return resolve(merged)
            if "allOf" in node and len(node["allOf"]) == 1:
                merged = {**resolve(node.pop("allOf")[0]), **node}
                return resolve(merged)
            if "anyOf" in node:
                node["anyOf"] = [resolve(item) for item in node["anyOf"]]
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


# --- transition resolvers ---------------------------------------------------
def _confirm_person_transition(args: ConfirmPersonArgs) -> ConversationState:
    return ConversationState.DISCOVERY if args.is_correct_person else ConversationState.END_CALL


def _resolve_objection_transition(args: ResolveObjectionArgs) -> ConversationState:
    return ConversationState.PITCH if args.resolved else ConversationState.OBJECTION_HANDLING


def _static(state: ConversationState):
    def _transition(_args: ToolArgs) -> ConversationState:
        return state

    return _transition


def _confirm_person_outcome(args: ConfirmPersonArgs) -> CallOutcome | None:
    if args.is_correct_person:
        return None
    reason = (args.reason or "").lower()
    return (
        CallOutcome.UNAVAILABLE
        if "unavailab" in reason or "busy" in reason
        else CallOutcome.WRONG_PERSON
    )


TOOL_SPECS: dict[str, ToolSpec] = {
    "confirm_person": ToolSpec(
        name="confirm_person",
        description=(
            "Record whether the person on the line is the owner or decision maker. "
            "Call this as soon as they answer the 'am I speaking with ...' question. "
            "Set is_correct_person=false if it is the wrong person, a wrong number, or "
            "they say the owner is unavailable."
        ),
        args_model=ConfirmPersonArgs,
        allowed_states=frozenset({ConversationState.CONFIRM_PERSON}),
        transition=_confirm_person_transition,
        outcome=_confirm_person_outcome,
        examples=("Yes this is the owner speaking", "He's not in right now"),
    ),
    "capture_discovery": ToolSpec(
        name="capture_discovery",
        description=(
            "Record what you learned about how the business currently gets customers, then "
            "move on to the pitch. Call this once you have one concrete answer — do not "
            "interrogate the lead."
        ),
        args_model=CaptureDiscoveryArgs,
        allowed_states=frozenset({ConversationState.DISCOVERY}),
        transition=_static(ConversationState.PITCH),
        examples=("Mostly walk-ins and some Instagram",),
    ),
    "classify_objection": ToolSpec(
        name="classify_objection",
        description=(
            "Bucket an objection the lead raised so it can be handled and logged. Call this "
            "the moment the lead pushes back on price, timing, budget, interest, or asks for "
            "time to think."
        ),
        args_model=ClassifyObjectionArgs,
        allowed_states=frozenset(
            {
                ConversationState.PITCH,
                ConversationState.OBJECTION_HANDLING,
                ConversationState.CLOSE,
            }
        ),
        transition=_static(ConversationState.OBJECTION_HANDLING),
        examples=("That sounds expensive", "Not right now, maybe next quarter"),
    ),
    "resolve_objection": ToolSpec(
        name="resolve_objection",
        description=(
            "Record the outcome of handling an objection. resolved=true returns the "
            "conversation to the pitch; resolved=false keeps handling it."
        ),
        args_model=ResolveObjectionArgs,
        allowed_states=frozenset({ConversationState.OBJECTION_HANDLING}),
        transition=_resolve_objection_transition,
    ),
    "move_to_close": ToolSpec(
        name="move_to_close",
        description=(
            "The lead has shown genuine interest — move to asking for the meeting. Only call "
            "this on a real buying signal, never to escape an unanswered objection."
        ),
        args_model=MoveToCloseArgs,
        allowed_states=frozenset({ConversationState.PITCH, ConversationState.OBJECTION_HANDLING}),
        transition=_static(ConversationState.CLOSE),
        examples=("Okay, how would that work for us?",),
    ),
    "schedule_meeting": ToolSpec(
        name="schedule_meeting",
        description=(
            "Book the follow-up meeting once the lead has agreed to a specific day and time "
            "and confirmed a contact detail for the invite."
        ),
        args_model=ScheduleMeetingArgs,
        allowed_states=frozenset({ConversationState.CLOSE}),
        transition=_static(ConversationState.BOOK_MEETING),
        outcome=CallOutcome.MEETING_BOOKED,
        examples=("Thursday at 4 works",),
    ),
    "mark_not_interested": ToolSpec(
        name="mark_not_interested",
        description=(
            "Log a hard no and stop selling. Call this when the lead has clearly declined "
            "after an objection was addressed, or asks not to be contacted."
        ),
        args_model=MarkNotInterestedArgs,
        allowed_states=frozenset(
            {
                ConversationState.OBJECTION_HANDLING,
                ConversationState.CLOSE,
                ConversationState.PITCH,
            }
        ),
        transition=_static(ConversationState.END_CALL),
        outcome=CallOutcome.NOT_INTERESTED,
        terminal=True,
    ),
    "end_call": ToolSpec(
        name="end_call",
        description=(
            "End the call and log the outcome. Use after booking a meeting, after a hard no, "
            "or when the conversation cannot continue."
        ),
        args_model=EndCallArgs,
        allowed_states=ANY_STATE,
        transition=_static(ConversationState.END_CALL),
        outcome=lambda args: args.outcome,
        terminal=True,
    ),
    "log_voicemail": ToolSpec(
        name="log_voicemail",
        description="Answering machine or voicemail detected — log it and hang up without pitching.",
        args_model=LogVoicemailArgs,
        allowed_states=ANY_STATE,
        transition=_static(ConversationState.END_CALL),
        outcome=CallOutcome.VOICEMAIL,
        terminal=True,
    ),
}


def get_tool_spec(name: str) -> ToolSpec | None:
    return TOOL_SPECS.get(name)


def tools_for_state(state: ConversationState) -> list[ToolSpec]:
    """Tools the LLM is allowed to call while in `state` (stable ordering)."""
    return [spec for spec in TOOL_SPECS.values() if state in spec.allowed_states]


def tool_schemas_for_state(state: ConversationState) -> list[dict[str, Any]]:
    return [spec.json_schema() for spec in tools_for_state(state)]


def tool_names_for_state(state: ConversationState) -> set[str]:
    return {spec.name for spec in tools_for_state(state)}


__all__ = [
    "ANY_STATE",
    "TOOL_SPECS",
    "ToolSpec",
    "get_tool_spec",
    "tool_names_for_state",
    "tool_schemas_for_state",
    "tools_for_state",
]
