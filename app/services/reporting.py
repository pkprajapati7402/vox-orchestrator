"""Reporting: the weekly cost / outcome summary from Phase 5.

`build_summary()` answers the three questions that matter for the agency:
total spend, cost per call, and cost per booked meeting — plus the outcome
breakdown behind them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import CostRepository
from app.enums import CallOutcome


async def build_summary(session: AsyncSession, days: int | None = 7) -> dict[str, Any]:
    """Aggregate cost + outcome statistics for the last `days` (None = all time)."""
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    summary = await CostRepository(session).summary(since)
    summary["window_days"] = days
    summary["generated_at"] = datetime.now(UTC).isoformat()

    outcomes: dict[str, int] = summary.get("outcomes", {})
    total_calls = sum(outcomes.values())
    summary["total_calls"] = total_calls
    summary["connect_rate"] = (
        round(
            sum(
                count
                for outcome, count in outcomes.items()
                if outcome
                not in {
                    CallOutcome.NO_ANSWER.value,
                    CallOutcome.BUSY.value,
                    CallOutcome.VOICEMAIL.value,
                    CallOutcome.FAILED.value,
                }
            )
            / total_calls,
            4,
        )
        if total_calls
        else 0.0
    )
    summary["booking_rate"] = (
        round(outcomes.get(CallOutcome.MEETING_BOOKED.value, 0) / total_calls, 4)
        if total_calls
        else 0.0
    )
    return summary


def render_summary_markdown(summary: dict[str, Any]) -> str:
    """Human-readable version of `build_summary` (used by the CLI and docs)."""
    window = summary.get("window_days")
    lines = [
        f"# Call cost & outcome summary ({'all time' if not window else f'last {window} days'})",
        "",
        f"- Generated: `{summary.get('generated_at')}`",
        f"- Calls: **{summary.get('total_calls', 0)}** (costed: {summary.get('calls_costed', 0)})",
        f"- Total spend: **${summary.get('total_cost_usd', 0):.4f}**",
        f"- Avg cost / call: **${summary.get('avg_cost_per_call_usd', 0):.4f}**",
        f"- Meetings booked: **{summary.get('meetings_booked', 0)}**",
    ]
    cost_per_meeting = summary.get("cost_per_booked_meeting_usd")
    lines.append(
        "- Cost / booked meeting: **"
        + (f"${cost_per_meeting:.4f}" if cost_per_meeting is not None else "n/a")
        + "**"
    )
    lines += [
        f"- Booking rate: **{summary.get('booking_rate', 0) * 100:.1f}%**",
        f"- Avg call duration: **{summary.get('avg_call_duration_seconds', 0):.1f}s**",
        "",
        "## Outcomes",
        "",
        "| Outcome | Calls |",
        "|---|---:|",
    ]
    for outcome, count in sorted(summary.get("outcomes", {}).items(), key=lambda item: -item[1]):
        lines.append(f"| {outcome} | {count} |")

    totals = summary.get("totals", {})
    lines += [
        "",
        "## Resource usage",
        "",
        "| Resource | Total |",
        "|---|---:|",
        f"| STT seconds | {totals.get('stt_seconds', 0):.0f} |",
        f"| LLM input tokens | {totals.get('llm_input_tokens', 0)} |",
        f"| LLM output tokens | {totals.get('llm_output_tokens', 0)} |",
        f"| TTS characters | {totals.get('tts_characters', 0)} |",
    ]
    return "\n".join(lines) + "\n"


__all__ = ["build_summary", "render_summary_markdown"]
