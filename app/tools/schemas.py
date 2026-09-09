"""Pydantic argument schemas for every agent tool (Project-Details.md §5).

`Extra` fields are forbidden and every field is strictly typed: a model that
invents an argument, drops a required one, or passes the wrong type produces a
`ValidationError`, which the registry turns into a bounded retry rather than a
silent state transition.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from datetime import date as date_cls
from datetime import time as time_cls

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums import CallOutcome, ObjectionType

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ToolArgs(BaseModel):
    """Base class: strict, no unknown keys, no coercion surprises."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class ConfirmPersonArgs(ToolArgs):
    """`confirm_person` — is the decision maker actually on the line?"""

    is_correct_person: bool = Field(
        description="True only if the person on the line is the owner/decision maker."
    )
    contact_name: str | None = Field(
        default=None, max_length=160, description="Name they gave, if any."
    )
    reason: str | None = Field(
        default=None,
        max_length=300,
        description="Why not, when is_correct_person is false (wrong number, unavailable, ...).",
    )


class CaptureDiscoveryArgs(ToolArgs):
    """`capture_discovery` — Discovery → Pitch, records what we learned."""

    current_marketing: str = Field(
        min_length=2,
        max_length=400,
        description="How the business currently gets customers (their words).",
    )
    pain_point: str | None = Field(default=None, max_length=400)
    decision_authority: bool | None = Field(
        default=None, description="Whether this contact can approve spend."
    )


class ClassifyObjectionArgs(ToolArgs):
    """`classify_objection` — bucket the objection so it can be handled and scored."""

    type: ObjectionType = Field(description="Objection bucket.")
    verbatim: str | None = Field(
        default=None, max_length=400, description="What the lead actually said."
    )


class ResolveObjectionArgs(ToolArgs):
    """`resolve_objection` — ObjectionHandling → Pitch once the objection is answered."""

    objection_type: ObjectionType
    resolved: bool = Field(description="True if the lead accepted the rebuttal.")
    rebuttal_summary: str | None = Field(default=None, max_length=400)


class MoveToCloseArgs(ToolArgs):
    """`move_to_close` — Pitch/ObjectionHandling → Close on a positive signal."""

    interest_signal: str = Field(
        min_length=2, max_length=300, description="The buying signal that justifies closing."
    )


class ScheduleMeetingArgs(ToolArgs):
    """`schedule_meeting` — Close → BookMeeting."""

    date: str = Field(description="Meeting date, ISO `YYYY-MM-DD`.")
    time: str = Field(description="Meeting start time, 24h `HH:MM` IST.")
    contact_confirmation: str = Field(
        min_length=3,
        max_length=200,
        description="Contact detail the lead confirmed for the invite (name + phone or email).",
    )
    duration_minutes: int = Field(default=30, ge=10, le=120)
    notes: str | None = Field(default=None, max_length=400)

    @field_validator("date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not _DATE_RE.match(v):
            raise ValueError("date must be formatted YYYY-MM-DD")
        try:
            parsed = date_cls.fromisoformat(v)
        except ValueError as exc:  # pragma: no cover - guarded by regex
            raise ValueError("date is not a real calendar date") from exc
        if parsed < datetime.now(UTC).date():
            raise ValueError("meeting date cannot be in the past")
        return v

    @field_validator("time")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError("time must be 24-hour HH:MM")
        return v

    def to_datetime(self) -> datetime:
        """Combine date+time into a UTC-aware datetime (input is treated as IST)."""
        naive = datetime.combine(
            date_cls.fromisoformat(self.date), time_cls.fromisoformat(self.time)
        )
        try:
            from zoneinfo import ZoneInfo

            return naive.replace(tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(UTC)
        except Exception:  # pragma: no cover - tzdata missing
            return naive.replace(tzinfo=UTC)


class MarkNotInterestedArgs(ToolArgs):
    """`mark_not_interested` — logs a hard no."""

    reason: str = Field(min_length=2, max_length=400)
    objection_type: ObjectionType | None = None
    can_follow_up_later: bool = False


class EndCallArgs(ToolArgs):
    """`end_call` — ends the call and records the outcome. Callable from any state."""

    outcome: CallOutcome
    reason: str = Field(min_length=2, max_length=400)

    @field_validator("outcome")
    @classmethod
    def _terminal_outcome(cls, v: CallOutcome) -> CallOutcome:
        if v is CallOutcome.IN_PROGRESS:
            raise ValueError("end_call requires a terminal outcome, not 'in_progress'")
        return v


class LogVoicemailArgs(ToolArgs):
    """`log_voicemail` — AMD detected a machine; skip the conversation."""

    left_message: bool = False
    detail: str | None = Field(default=None, max_length=300)


__all__ = [
    "CaptureDiscoveryArgs",
    "ClassifyObjectionArgs",
    "ConfirmPersonArgs",
    "EndCallArgs",
    "LogVoicemailArgs",
    "MarkNotInterestedArgs",
    "MoveToCloseArgs",
    "ResolveObjectionArgs",
    "ScheduleMeetingArgs",
    "ToolArgs",
]
