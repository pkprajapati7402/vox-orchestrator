"""Scoring for the eval harness (Project-Details.md §9).

Four metrics, deliberately blunt and easy to defend:

  task_completion            the call ended in the outcome the persona label says it should
  correct_tool_sequence      every required tool fired, in order, and no forbidden tool did
  hallucinated_tool_call     the model tried a tool that does not exist or is illegal here
  turns_to_resolution        how many lead turns it took to get there

`correct_tool_sequence` uses subsequence matching, not equality: the agent is
allowed to take extra valid steps, but not to skip a mandated one or reorder it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import mean
from typing import Any

from app.enums import CallOutcome
from eval.personas import Persona


def is_subsequence(required: Sequence[str], actual: Sequence[str]) -> bool:
    """True if `required` appears in `actual` in order (gaps allowed)."""
    iterator = iter(actual)
    return all(any(item == candidate for candidate in iterator) for item in required)


@dataclass
class CaseResult:
    """Score for one persona conversation."""

    persona_id: str
    persona_name: str
    category: str
    difficulty: str
    expected_outcome: str
    actual_outcome: str | None
    final_state: str
    tool_sequence: list[str]
    rejected_tool_calls: list[dict[str, Any]]
    turns_to_resolution: int
    hallucinated_tool_call: bool
    task_completed: bool
    correct_tool_sequence: bool
    fallback_lines_used: int
    objection_cycles: int
    cost_usd: float
    failure_notes: list[str] = field(default_factory=list)
    transcript: str = ""

    @property
    def passed(self) -> bool:
        return (
            self.task_completed and self.correct_tool_sequence and not self.hallucinated_tool_call
        )

    def as_dict(self, include_transcript: bool = False) -> dict[str, Any]:
        payload = {
            "persona_id": self.persona_id,
            "persona_name": self.persona_name,
            "category": self.category,
            "difficulty": self.difficulty,
            "expected_outcome": self.expected_outcome,
            "actual_outcome": self.actual_outcome,
            "final_state": self.final_state,
            "tool_sequence": self.tool_sequence,
            "rejected_tool_calls": self.rejected_tool_calls,
            "turns_to_resolution": self.turns_to_resolution,
            "hallucinated_tool_call": self.hallucinated_tool_call,
            "task_completed": self.task_completed,
            "correct_tool_sequence": self.correct_tool_sequence,
            "fallback_lines_used": self.fallback_lines_used,
            "objection_cycles": self.objection_cycles,
            "cost_usd": self.cost_usd,
            "passed": self.passed,
            "failure_notes": self.failure_notes,
        }
        if include_transcript:
            payload["transcript"] = self.transcript
        return payload


def score_case(persona: Persona, metrics: dict[str, Any], transcript: str = "") -> CaseResult:
    """Turn one engine run into a scored `CaseResult`."""
    actual_outcome = metrics.get("outcome")
    expected_outcome = persona.expected_outcome.value
    tool_sequence: list[str] = list(metrics.get("tool_sequence") or [])
    notes: list[str] = []

    task_completed = actual_outcome == expected_outcome
    if not task_completed:
        notes.append(f"expected outcome '{expected_outcome}', got '{actual_outcome}'")

    missing = [tool for tool in persona.required_tools if tool not in tool_sequence]
    if missing:
        notes.append(f"missing required tool(s): {', '.join(missing)}")
    ordered = is_subsequence(persona.required_tools, tool_sequence)
    if not ordered and not missing:
        notes.append("required tools fired out of order")
    forbidden = [tool for tool in persona.forbidden_tools if tool in tool_sequence]
    if forbidden:
        notes.append(f"forbidden tool(s) called: {', '.join(forbidden)}")

    hallucinated = int(metrics.get("hallucinated_tool_calls") or 0) > 0
    if hallucinated:
        rejected = metrics.get("rejected_tool_calls") or []
        names = ", ".join(sorted({item.get("tool", "?") for item in rejected}))
        notes.append(f"hallucinated tool call(s): {names}")

    correct_sequence = not missing and ordered and not forbidden

    return CaseResult(
        persona_id=persona.id,
        persona_name=persona.name,
        category=persona.category,
        difficulty=persona.difficulty,
        expected_outcome=expected_outcome,
        actual_outcome=actual_outcome,
        final_state=str(metrics.get("final_state")),
        tool_sequence=tool_sequence,
        rejected_tool_calls=list(metrics.get("rejected_tool_calls") or []),
        turns_to_resolution=int(metrics.get("turns") or 0),
        hallucinated_tool_call=hallucinated,
        task_completed=task_completed,
        correct_tool_sequence=correct_sequence,
        fallback_lines_used=int(metrics.get("fallback_lines_used") or 0),
        objection_cycles=int(metrics.get("objection_cycles") or 0),
        cost_usd=float((metrics.get("cost") or {}).get("cost_usd") or 0.0),
        failure_notes=notes,
        transcript=transcript,
    )


@dataclass
class SuiteScore:
    """Aggregate metrics across every persona in a run."""

    personas_run: int
    task_completion_rate: float
    correct_tool_sequence_rate: float
    hallucinated_tool_call_rate: float
    avg_turns_to_resolution: float
    pass_rate: float
    total_cost_usd: float
    avg_cost_usd: float
    booking_rate: float
    by_category: dict[str, dict[str, float]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "personas_run": self.personas_run,
            "task_completion_rate": self.task_completion_rate,
            "correct_tool_sequence_rate": self.correct_tool_sequence_rate,
            "hallucinated_tool_call_rate": self.hallucinated_tool_call_rate,
            "avg_turns_to_resolution": self.avg_turns_to_resolution,
            "pass_rate": self.pass_rate,
            "total_cost_usd": self.total_cost_usd,
            "avg_cost_usd": self.avg_cost_usd,
            "booking_rate": self.booking_rate,
            "by_category": self.by_category,
        }


def aggregate(results: Sequence[CaseResult]) -> SuiteScore:
    total = len(results)
    if total == 0:
        return SuiteScore(0, 0, 0, 0, 0, 0, 0, 0, 0, {})

    def rate(predicate) -> float:  # noqa: ANN001
        return round(sum(1 for r in results if predicate(r)) / total, 4)

    by_category: dict[str, dict[str, float]] = {}
    categories = sorted({r.category for r in results})
    for category in categories:
        subset = [r for r in results if r.category == category]
        by_category[category] = {
            "count": len(subset),
            "task_completion_rate": round(
                sum(1 for r in subset if r.task_completed) / len(subset), 4
            ),
            "correct_tool_sequence_rate": round(
                sum(1 for r in subset if r.correct_tool_sequence) / len(subset), 4
            ),
            "hallucinated_tool_call_rate": round(
                sum(1 for r in subset if r.hallucinated_tool_call) / len(subset), 4
            ),
            "avg_turns_to_resolution": round(mean(r.turns_to_resolution for r in subset), 2),
        }

    total_cost = sum(r.cost_usd for r in results)
    return SuiteScore(
        personas_run=total,
        task_completion_rate=rate(lambda r: r.task_completed),
        correct_tool_sequence_rate=rate(lambda r: r.correct_tool_sequence),
        hallucinated_tool_call_rate=rate(lambda r: r.hallucinated_tool_call),
        avg_turns_to_resolution=round(mean(r.turns_to_resolution for r in results), 2),
        pass_rate=rate(lambda r: r.passed),
        total_cost_usd=round(total_cost, 6),
        avg_cost_usd=round(total_cost / total, 6),
        booking_rate=rate(lambda r: r.actual_outcome == CallOutcome.MEETING_BOOKED.value),
        by_category=by_category,
    )


def compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Delta between two suite scores — the regression detector."""
    keys = (
        "task_completion_rate",
        "correct_tool_sequence_rate",
        "hallucinated_tool_call_rate",
        "avg_turns_to_resolution",
        "pass_rate",
    )
    deltas = {}
    for key in keys:
        before = float(baseline.get(key, 0) or 0)
        after = float(candidate.get(key, 0) or 0)
        deltas[key] = {
            "baseline": before,
            "candidate": after,
            "delta": round(after - before, 4),
        }
    regressed = (
        deltas["task_completion_rate"]["delta"] < 0
        or deltas["correct_tool_sequence_rate"]["delta"] < 0
        or deltas["hallucinated_tool_call_rate"]["delta"] > 0
    )
    return {"deltas": deltas, "regressed": regressed}


__all__ = [
    "CaseResult",
    "SuiteScore",
    "aggregate",
    "compare",
    "is_subsequence",
    "score_case",
]
