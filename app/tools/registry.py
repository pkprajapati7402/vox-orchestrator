"""Validating tool registry.

Contract enforced here (Project-Details.md §5/§7):

1. The tool must exist.
2. The tool must be callable **from the current state** — a `schedule_meeting`
   emitted during Greeting is a hallucination, not a transition.
3. The arguments must validate against the tool's Pydantic schema.

Only when all three hold does `execute_tool` return a transition. Anything else
raises `ToolValidationError`, which the engine converts into one bounded retry
(with the validation error appended to the LLM context) and then a scripted
safe line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.enums import CallOutcome, ConversationState
from app.tools.definitions import TOOL_SPECS, ToolSpec, get_tool_spec, tool_names_for_state
from app.tools.schemas import ToolArgs


class ToolValidationError(ValueError):
    """Raised when a tool call is unknown, illegal in this state, or malformed."""

    def __init__(self, message: str, *, kind: str, tool_name: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind  # unknown_tool | illegal_state | invalid_args
        self.tool_name = tool_name

    @property
    def is_hallucination(self) -> bool:
        """Unknown tools and out-of-state calls count as hallucinations in eval."""
        return self.kind in {"unknown_tool", "illegal_state"}

    def feedback(self, state: ConversationState) -> str:
        """Message appended to the LLM context for the retry attempt."""
        allowed = ", ".join(sorted(tool_names_for_state(state))) or "none"
        return (
            f"Your previous tool call was rejected: {self}. "
            f"Tools callable from state '{state.value}': {allowed}. "
            "Re-issue exactly one valid tool call, or reply with speech only."
        )


@dataclass
class ToolInvocation:
    """Result of a successfully validated (and executed) tool call."""

    spec: ToolSpec
    args: ToolArgs
    from_state: ConversationState
    to_state: ConversationState
    outcome: CallOutcome | None = None
    terminal: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def args_dict(self) -> dict[str, Any]:
        return self.args.model_dump(mode="json")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ToolInvocation {self.name} {self.from_state.value}->{self.to_state.value}>"


def validate_tool_call(
    name: str, raw_args: dict[str, Any] | None, state: ConversationState
) -> tuple[ToolSpec, ToolArgs]:
    """Validate name + state legality + argument schema. Raises `ToolValidationError`."""
    spec = get_tool_spec(name)
    if spec is None:
        raise ToolValidationError(
            f"unknown tool '{name}' (known tools: {', '.join(sorted(TOOL_SPECS))})",
            kind="unknown_tool",
            tool_name=name,
        )
    if state not in spec.allowed_states:
        raise ToolValidationError(
            f"tool '{name}' is not callable from state '{state.value}'",
            kind="illegal_state",
            tool_name=name,
        )
    try:
        args = spec.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ToolValidationError(
            f"invalid arguments for '{name}': {detail}", kind="invalid_args", tool_name=name
        ) from exc
    return spec, args


def resolve_outcome(spec: ToolSpec, args: ToolArgs) -> CallOutcome | None:
    """Outcome implied by a tool call (may be static, callable, or absent)."""
    outcome = spec.outcome
    if outcome is None:
        return None
    if callable(outcome) and not isinstance(outcome, CallOutcome):
        return outcome(args)
    return outcome  # type: ignore[return-value]


def execute_tool(
    name: str, raw_args: dict[str, Any] | None, state: ConversationState
) -> ToolInvocation:
    """Validate a tool call and compute the resulting transition."""
    spec, args = validate_tool_call(name, raw_args, state)
    to_state = spec.transition(args)
    outcome = resolve_outcome(spec, args)
    terminal = spec.terminal or to_state is ConversationState.END_CALL
    metadata: dict[str, Any] = {}

    if name == "schedule_meeting":
        metadata["meeting_at"] = args.to_datetime()  # type: ignore[attr-defined]
        metadata["meeting_contact"] = args.contact_confirmation  # type: ignore[attr-defined]
    elif name == "classify_objection":
        metadata["objection_type"] = args.type.value  # type: ignore[attr-defined]
    elif name == "resolve_objection":
        metadata["objection_type"] = args.objection_type.value  # type: ignore[attr-defined]
        metadata["resolved"] = args.resolved  # type: ignore[attr-defined]
    elif name == "mark_not_interested" and args.objection_type:  # type: ignore[attr-defined]
        metadata["objection_type"] = args.objection_type.value  # type: ignore[attr-defined]

    return ToolInvocation(
        spec=spec,
        args=args,
        from_state=state,
        to_state=to_state,
        outcome=outcome,
        terminal=terminal,
        metadata=metadata,
    )


__all__ = [
    "ToolInvocation",
    "ToolValidationError",
    "execute_tool",
    "resolve_outcome",
    "validate_tool_call",
]
