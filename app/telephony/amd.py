"""Answering-machine detection and the unanswered-call backoff schedule (§7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.enums import AnsweredBy, CallOutcome


@dataclass(frozen=True)
class AMDDecision:
    """What to do with a call based on Twilio's `AnsweredBy` value."""

    answered_by: AnsweredBy
    is_machine: bool
    should_hang_up: bool
    outcome: CallOutcome | None
    reason: str


def classify_answered_by(raw: str | None) -> AMDDecision:
    """Map Twilio's AMD result to an action.

    `machine_start` means AMD fired *before* the greeting finished — we hang up
    and log a voicemail rather than pitching an answering machine. `unknown`
    means AMD timed out; we proceed with the conversation, since a real human
    on a noisy line is the more common cause.
    """
    try:
        answered_by = AnsweredBy(str(raw or "unknown").lower())
    except ValueError:
        answered_by = AnsweredBy.UNKNOWN

    if answered_by is AnsweredBy.HUMAN:
        return AMDDecision(answered_by, False, False, None, "human answered")
    if answered_by is AnsweredBy.UNKNOWN:
        return AMDDecision(
            answered_by, False, False, None, "AMD inconclusive — continuing as human"
        )
    if answered_by is AnsweredBy.FAX:
        return AMDDecision(answered_by, True, True, CallOutcome.FAILED, "fax machine answered")
    return AMDDecision(
        answered_by,
        True,
        True,
        CallOutcome.VOICEMAIL,
        f"answering machine detected ({answered_by.value})",
    )


def backoff_delay(attempt_number: int, schedule: list[int] | None = None) -> timedelta | None:
    """Delay before attempt `attempt_number + 1`, or None when attempts are exhausted.

    `attempt_number` is 1-based: the delay after the first attempt is `schedule[0]`.
    """
    settings = get_settings()
    schedule = schedule if schedule is not None else settings.retry_backoff_minutes
    index = max(attempt_number, 1) - 1
    if index >= len(schedule) or attempt_number >= settings.call_max_attempts:
        return None
    return timedelta(minutes=schedule[index])


def next_attempt_at(
    attempt_number: int, *, now: datetime | None = None, schedule: list[int] | None = None
) -> datetime | None:
    """Absolute UTC timestamp for the next retry, or None if there is none."""
    delay = backoff_delay(attempt_number, schedule)
    if delay is None:
        return None
    return (now or datetime.now(UTC)) + delay


__all__ = ["AMDDecision", "backoff_delay", "classify_answered_by", "next_attempt_at"]
