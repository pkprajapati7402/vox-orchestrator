"""Shared domain enums.

Kept in a dependency-free module so the DB layer, the tool schemas, the flow
state machine and the eval harness can all import them without cycles.
"""

from __future__ import annotations

from enum import StrEnum


class ConversationState(StrEnum):
    """States of the conversation state machine (Project-Details.md §4)."""

    GREETING = "greeting"
    CONFIRM_PERSON = "confirm_person"
    DISCOVERY = "discovery"
    PITCH = "pitch"
    OBJECTION_HANDLING = "objection_handling"
    CLOSE = "close"
    BOOK_MEETING = "book_meeting"
    END_CALL = "end_call"
    WRAPUP = "wrapup"

    @property
    def is_terminal(self) -> bool:
        return self is ConversationState.WRAPUP


class CallOutcome(StrEnum):
    """Terminal outcome of a call, stored on `calls.outcome`."""

    MEETING_BOOKED = "meeting_booked"
    CALLBACK_REQUESTED = "callback_requested"
    NOT_INTERESTED = "not_interested"
    WRONG_PERSON = "wrong_person"
    UNAVAILABLE = "unavailable"
    VOICEMAIL = "voicemail"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    HUNG_UP = "hung_up"
    FAILED = "failed"
    IN_PROGRESS = "in_progress"

    @property
    def is_success(self) -> bool:
        return self is CallOutcome.MEETING_BOOKED

    @property
    def is_retryable(self) -> bool:
        """Outcomes that justify another attempt on the backoff schedule."""
        return self in {
            CallOutcome.NO_ANSWER,
            CallOutcome.VOICEMAIL,
            CallOutcome.BUSY,
            CallOutcome.UNAVAILABLE,
            CallOutcome.FAILED,
        }


class ObjectionType(StrEnum):
    """Objection buckets (Project-Details.md §5, `classify_objection`)."""

    PRICE = "price"
    TIMING = "timing"
    NOT_INTERESTED = "not_interested"
    NEED_TO_THINK = "need_to_think"
    NO_BUDGET = "no_budget"


class TurnRole(StrEnum):
    AGENT = "agent"
    LEAD = "lead"
    SYSTEM = "system"


class LeadStatus(StrEnum):
    NEW = "new"
    RESEARCHED = "researched"
    QUEUED = "queued"
    CALLING = "calling"
    CONTACTED = "contacted"
    BOOKED = "booked"
    REJECTED = "rejected"
    DO_NOT_CALL = "do_not_call"
    EXHAUSTED = "exhausted"


class AnsweredBy(StrEnum):
    """Twilio AMD `AnsweredBy` values."""

    HUMAN = "human"
    MACHINE_START = "machine_start"
    MACHINE_END_BEEP = "machine_end_beep"
    MACHINE_END_SILENCE = "machine_end_silence"
    MACHINE_END_OTHER = "machine_end_other"
    FAX = "fax"
    UNKNOWN = "unknown"

    @property
    def is_machine(self) -> bool:
        return self.value.startswith("machine") or self is AnsweredBy.FAX


class Provider(StrEnum):
    GROQ = "groq"
    GEMINI = "gemini"
    DEEPGRAM = "deepgram"
    ELEVENLABS = "elevenlabs"
    PIPER = "piper"
    TWILIO = "twilio"
    MOCK = "mock"
