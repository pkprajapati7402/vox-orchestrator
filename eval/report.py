"""Markdown rendering for eval reports (the artifact that goes in the README/PR)."""

from __future__ import annotations

from typing import Any

from eval.harness import EvalReport
from eval.scoring import compare


def render_report(report: EvalReport, *, max_failures: int = 15) -> str:
    score = report.score
    lines = [
        f"# Eval report — `{report.label}`",
        "",
        f"- Prompt variant: `{report.prompt_variant}`",
        f"- Agent LLM: `{report.agent_provider}` · Caller: `{report.caller}`",
        f"- Commit: `{report.git_sha or 'n/a'}`",
        f"- Run: `{report.started_at}` → `{report.finished_at}`",
        "",
        "## Headline metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Personas run | {score.personas_run} |",
        f"| Task completion rate | **{score.task_completion_rate * 100:.1f}%** |",
        f"| Correct tool sequence rate | **{score.correct_tool_sequence_rate * 100:.1f}%** |",
        f"| Hallucinated tool-call rate | **{score.hallucinated_tool_call_rate * 100:.1f}%** |",
        f"| Avg turns to resolution | **{score.avg_turns_to_resolution}** |",
        f"| Fully passing cases | {score.pass_rate * 100:.1f}% |",
        f"| Booking rate | {score.booking_rate * 100:.1f}% |",
        f"| Simulated cost (total / call) | ${score.total_cost_usd:.4f} / ${score.avg_cost_usd:.4f} |",
        "",
        "## By persona category",
        "",
        "| Category | N | Task completion | Correct tools | Hallucination | Avg turns |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for category, stats in sorted(score.by_category.items()):
        lines.append(
            f"| {category} | {int(stats['count'])} | "
            f"{stats['task_completion_rate'] * 100:.0f}% | "
            f"{stats['correct_tool_sequence_rate'] * 100:.0f}% | "
            f"{stats['hallucinated_tool_call_rate'] * 100:.0f}% | "
            f"{stats['avg_turns_to_resolution']} |"
        )

    failures = report.failures
    lines += ["", f"## Failures ({len(failures)})", ""]
    if not failures:
        lines.append(
            "None — every persona reached its expected outcome with a valid tool sequence."
        )
    else:
        lines += ["| Persona | Expected | Actual | Notes |", "|---|---|---|---|"]
        for result in failures[:max_failures]:
            notes = "; ".join(result.failure_notes).replace("|", "/")
            lines.append(
                f"| `{result.persona_id}` {result.persona_name} | {result.expected_outcome} | "
                f"{result.actual_outcome} | {notes} |"
            )
        if len(failures) > max_failures:
            lines.append(f"| ... | | | {len(failures) - max_failures} more |")

    lines += [
        "",
        "## Per-persona detail",
        "",
        "| Persona | Outcome | Turns | Tools | Pass |",
        "|---|---|---:|---|:--:|",
    ]
    for result in report.results:
        tools = " → ".join(result.tool_sequence) or "—"
        lines.append(
            f"| `{result.persona_id}` | {result.actual_outcome} | {result.turns_to_resolution} | "
            f"{tools} | {'✅' if result.passed else '❌'} |"
        )
    return "\n".join(lines) + "\n"


def render_comparison(baseline: dict[str, Any], candidate: dict[str, Any]) -> str:
    """Before/after table used to prove the harness detects regressions."""
    base_score = baseline.get("score", baseline)
    cand_score = candidate.get("score", candidate)
    result = compare(base_score, cand_score)

    lines = [
        "# Eval comparison",
        "",
        f"- Baseline: `{baseline.get('label', '?')}` "
        f"(prompt `{baseline.get('prompt_variant', '?')}`)",
        f"- Candidate: `{candidate.get('label', '?')}` "
        f"(prompt `{candidate.get('prompt_variant', '?')}`)",
        "",
        "| Metric | Baseline | Candidate | Δ |",
        "|---|---:|---:|---:|",
    ]
    for metric, values in result["deltas"].items():
        delta = values["delta"]
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
        lines.append(
            f"| {metric} | {values['baseline']} | {values['candidate']} | {arrow} {delta:+.4f} |"
        )
    lines += [
        "",
        f"**Regression detected: {'YES' if result['regressed'] else 'no'}**",
        "",
    ]
    return "\n".join(lines)


__all__ = ["render_comparison", "render_report"]
