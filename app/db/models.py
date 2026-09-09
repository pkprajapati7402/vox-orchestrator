"""ORM models — a direct implementation of the ER diagram in Project-Details.md §6.

    LEADS ||--o{ CALLS
    CALLS ||--o{ TRANSCRIPT_TURNS
    CALLS ||--|| COST_LOGS
    CALLS ||--o{ EVAL_RESULTS

Two pragmatic additions on top of the spec:
  * `leads.status` / retry bookkeeping — needed by the backoff scheduler (§7)
  * `eval_runs` — groups `eval_results` rows so baseline-vs-candidate reports
    can be compared over time (Phase 6 exit criteria).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow
from app.enums import CallOutcome, ConversationState, LeadStatus, TurnRole


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Lead(Base):
    """A business lead sourced from the Google Maps list."""

    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("phone", name="uq_leads_phone"),
        Index("ix_leads_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    business_name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(120), default=None)
    phone: Mapped[str] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(String(512), default=None)
    website: Mapped[str | None] = mapped_column(String(512), default=None)
    contact_name: Mapped[str | None] = mapped_column(String(160), default=None)
    research_notes: Mapped[str | None] = mapped_column(Text, default=None)
    researched_at: Mapped[datetime | None] = mapped_column(default=None)

    status: Mapped[str] = mapped_column(String(32), default=LeadStatus.NEW.value)
    do_not_call: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    next_attempt_at: Mapped[datetime | None] = mapped_column(default=None)

    source: Mapped[str | None] = mapped_column(String(64), default="google_maps")
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    calls: Mapped[list[Call]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Lead {self.business_name!r} {self.phone}>"


class Call(Base):
    """One outbound dial attempt and the conversation it produced."""

    __tablename__ = "calls"
    __table_args__ = (
        Index("ix_calls_lead_id", "lead_id"),
        Index("ix_calls_outcome", "outcome"),
        UniqueConstraint("provider_call_sid", name="uq_calls_provider_call_sid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))

    provider_call_sid: Mapped[str | None] = mapped_column(String(64), default=None)
    direction: Mapped[str] = mapped_column(String(16), default="outbound")
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)

    outcome: Mapped[str] = mapped_column(String(32), default=CallOutcome.IN_PROGRESS.value)
    outcome_reason: Mapped[str | None] = mapped_column(Text, default=None)
    final_state: Mapped[str | None] = mapped_column(String(32), default=None)
    answered_by: Mapped[str | None] = mapped_column(String(32), default=None)

    meeting_at: Mapped[datetime | None] = mapped_column(default=None)
    meeting_contact: Mapped[str | None] = mapped_column(String(160), default=None)
    objection_types: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(default=None)
    ended_at: Mapped[datetime | None] = mapped_column(default=None)
    duration_seconds: Mapped[float | None] = mapped_column(Float, default=None)

    recording_url: Mapped[str | None] = mapped_column(String(512), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    lead: Mapped[Lead] = relationship(back_populates="calls", lazy="selectin")
    turns: Mapped[list[TranscriptTurn]] = relationship(
        back_populates="call",
        cascade="all, delete-orphan",
        order_by="TranscriptTurn.turn_index",
        lazy="selectin",
    )
    cost_log: Mapped[CostLog | None] = relationship(
        back_populates="call", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Call {self.id} outcome={self.outcome}>"


class TranscriptTurn(Base):
    """A single utterance (or tool invocation) inside a call."""

    __tablename__ = "transcript_turns"
    __table_args__ = (
        Index("ix_transcript_turns_call_id", "call_id"),
        UniqueConstraint("call_id", "turn_index", name="uq_transcript_turns_call_turn"),
        CheckConstraint("turn_index >= 0", name="turn_index_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"))

    turn_index: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16), default=TurnRole.AGENT.value)
    content: Mapped[str] = mapped_column(Text, default="")

    state: Mapped[str | None] = mapped_column(String(32), default=None)
    tool_called: Mapped[str | None] = mapped_column(String(64), default=None)
    tool_args: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    tool_valid: Mapped[bool | None] = mapped_column(Boolean, default=None)

    asr_confidence: Mapped[float | None] = mapped_column(Float, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    call: Mapped[Call] = relationship(back_populates="turns", lazy="selectin")


class CostLog(Base):
    """Per-call resource usage and the dollar cost derived from it (§8)."""

    __tablename__ = "cost_logs"
    __table_args__ = (UniqueConstraint("call_id", name="uq_cost_logs_call_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"))

    stt_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    llm_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    tts_characters: Mapped[int] = mapped_column(Integer, default=0)
    telephony_minutes: Mapped[float] = mapped_column(Float, default=0.0)

    stt_cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    llm_cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    tts_cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    telephony_cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)

    breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    call: Mapped[Call] = relationship(back_populates="cost_log", lazy="selectin")


class EvalRun(Base):
    """One execution of the offline eval suite."""

    __tablename__ = "eval_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    label: Mapped[str] = mapped_column(String(120), default="baseline")
    git_sha: Mapped[str | None] = mapped_column(String(64), default=None)
    prompt_version: Mapped[str | None] = mapped_column(String(64), default=None)
    llm_provider: Mapped[str | None] = mapped_column(String(32), default=None)

    personas_run: Mapped[int] = mapped_column(Integer, default=0)
    task_completion_rate: Mapped[float] = mapped_column(Float, default=0.0)
    correct_tool_sequence_rate: Mapped[float] = mapped_column(Float, default=0.0)
    hallucinated_tool_call_rate: Mapped[float] = mapped_column(Float, default=0.0)
    avg_turns_to_resolution: Mapped[float] = mapped_column(Float, default=0.0)

    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    results: Mapped[list[EvalResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class EvalResult(Base):
    """Score for a single simulated persona conversation."""

    __tablename__ = "eval_results"
    __table_args__ = (Index("ix_eval_results_run_id", "run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), default=None
    )
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("calls.id", ondelete="SET NULL"), default=None
    )

    persona: Mapped[str] = mapped_column(String(120))
    persona_category: Mapped[str | None] = mapped_column(String(64), default=None)
    expected_outcome: Mapped[str | None] = mapped_column(String(32), default=None)
    actual_outcome: Mapped[str | None] = mapped_column(String(32), default=None)

    task_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    correct_tool_sequence: Mapped[bool] = mapped_column(Boolean, default=False)
    hallucinated_tool_call: Mapped[bool] = mapped_column(Boolean, default=False)
    turns_to_resolution: Mapped[int] = mapped_column(Integer, default=0)

    final_state: Mapped[str | None] = mapped_column(
        String(32), default=ConversationState.WRAPUP.value
    )
    tool_sequence: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    failure_notes: Mapped[str | None] = mapped_column(Text, default=None)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    run: Mapped[EvalRun | None] = relationship(back_populates="results", lazy="selectin")


__all__ = [
    "Base",
    "Call",
    "CostLog",
    "EvalResult",
    "EvalRun",
    "Lead",
    "TranscriptTurn",
]
