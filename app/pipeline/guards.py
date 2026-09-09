"""Runtime guardrails for the failure modes in Project-Details.md §7.

Each guard is a tiny, independently testable object the engine consults; none of
them depend on the LLM, so their behaviour is deterministic under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.enums import CallOutcome, ConversationState


@dataclass
class ASRConfidenceGate:
    """Low-confidence transcript -> ask the caller to repeat, at most N times."""

    min_confidence: float = 0.55
    max_repeats: int = 1
    repeats_used: int = 0

    def should_ask_repeat(self, confidence: float | None) -> bool:
        if confidence is None:
            return False
        if confidence >= self.min_confidence:
            return False
        return self.repeats_used < self.max_repeats

    def register_repeat(self) -> None:
        self.repeats_used += 1

    def reset(self) -> None:
        self.repeats_used = 0


@dataclass
class SilenceGuard:
    """Silence -> prompt once ("are you still there?"), then hang up gracefully."""

    max_prompts: int = 1
    prompts_used: int = 0

    def next_action(self) -> str:
        """Returns ``"prompt"`` or ``"hangup"``."""
        if self.prompts_used < self.max_prompts:
            self.prompts_used += 1
            return "prompt"
        return "hangup"

    def reset(self) -> None:
        self.prompts_used = 0


@dataclass
class ObjectionLoopGuard:
    """More than `max_cycles` objections -> force Close, then force EndCall.

    Prevents the "rebut → new objection → rebut" loop from running forever.
    """

    max_cycles: int = 3
    cycles: int = 0
    forced_close_used: bool = False
    types_seen: list[str] = field(default_factory=list)

    def register(self, objection_type: str | None = None) -> None:
        self.cycles += 1
        if objection_type:
            self.types_seen.append(objection_type)

    @property
    def exceeded(self) -> bool:
        return self.cycles > self.max_cycles

    def forced_state(self) -> ConversationState | None:
        """State to force to, or None if the loop is still within budget."""
        if not self.exceeded:
            return None
        if not self.forced_close_used:
            self.forced_close_used = True
            return ConversationState.CLOSE
        return ConversationState.END_CALL


@dataclass
class TurnBudget:
    """Hard cap on conversation length, independent of what the model wants."""

    max_turns: int = 40
    turns: int = 0

    def register(self) -> None:
        self.turns += 1

    @property
    def exhausted(self) -> bool:
        return self.turns >= self.max_turns


@dataclass
class ToolRetryBudget:
    """Bounded retries for malformed/illegal tool calls before the scripted line."""

    max_retries: int = 1
    used: int = 0

    def can_retry(self) -> bool:
        return self.used < self.max_retries

    def consume(self) -> None:
        self.used += 1

    def reset(self) -> None:
        self.used = 0


def outcome_for_forced_end(state: ConversationState) -> CallOutcome:
    """Outcome assigned when a guard (not the model) terminates the call."""
    from app.pipeline.states import default_outcome

    return default_outcome(state)


__all__ = [
    "ASRConfidenceGate",
    "ObjectionLoopGuard",
    "SilenceGuard",
    "ToolRetryBudget",
    "TurnBudget",
    "outcome_for_forced_end",
]
