"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.enums import CallOutcome, LeadStatus


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    database: str
    cache: str
    providers: dict[str, Any]
    pipecat_installed: bool
    compliance: dict[str, Any]


class LeadCreate(BaseModel):
    business_name: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=5, max_length=32)
    category: str | None = None
    address: str | None = None
    website: str | None = None
    contact_name: str | None = None
    research_notes: str | None = None
    do_not_call: bool = False


class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    business_name: str
    phone: str
    category: str | None = None
    address: str | None = None
    website: str | None = None
    contact_name: str | None = None
    research_notes: str | None = None
    status: str = LeadStatus.NEW.value
    do_not_call: bool = False
    attempts: int = 0
    next_attempt_at: datetime | None = None
    researched_at: datetime | None = None
    created_at: datetime | None = None


class TurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    turn_index: int
    role: str
    content: str
    state: str | None = None
    tool_called: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_valid: bool | None = None
    asr_confidence: float | None = None
    latency_ms: int | None = None


class CostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stt_seconds: float = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    tts_characters: int = 0
    telephony_minutes: float = 0
    cost_usd: float = 0


class CallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID
    provider_call_sid: str | None = None
    outcome: str = CallOutcome.IN_PROGRESS.value
    outcome_reason: str | None = None
    final_state: str | None = None
    answered_by: str | None = None
    attempt_number: int = 1
    meeting_at: datetime | None = None
    objection_types: list[str] | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: float | None = None


class CallDetail(CallOut):
    turns: list[TurnOut] = Field(default_factory=list)
    cost: CostOut | None = None


class DialRequest(BaseModel):
    lead_id: uuid.UUID
    dry_run: bool = False


class DialResponse(BaseModel):
    call_id: uuid.UUID
    lead_id: uuid.UUID
    phone: str
    provider_call_sid: str | None = None
    status: str
    dry_run: bool = False


class CampaignRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=200)
    concurrency: int = Field(default=2, ge=1, le=10)
    dry_run: bool = False


class ResearchRequest(BaseModel):
    limit: int = Field(default=25, ge=1, le=200)
    force: bool = False


class SummaryResponse(BaseModel):
    window_days: int | None = None
    total_calls: int = 0
    calls_costed: int = 0
    total_cost_usd: float = 0
    avg_cost_per_call_usd: float = 0
    meetings_booked: int = 0
    cost_per_booked_meeting_usd: float | None = None
    booking_rate: float = 0
    connect_rate: float = 0
    avg_call_duration_seconds: float = 0
    totals: dict[str, Any] = Field(default_factory=dict)
    outcomes: dict[str, int] = Field(default_factory=dict)
    generated_at: str | None = None


__all__ = [
    "CallDetail",
    "CallOut",
    "CampaignRequest",
    "CostOut",
    "DialRequest",
    "DialResponse",
    "HealthResponse",
    "LeadCreate",
    "LeadOut",
    "ResearchRequest",
    "SummaryResponse",
    "TurnOut",
]
