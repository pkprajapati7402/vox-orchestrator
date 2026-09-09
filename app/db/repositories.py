"""Repository layer — every SQL query the app issues lives here.

Keeping persistence in one place means the conversation engine, the API and the
eval harness all share the same, tested read/write paths.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Call, CostLog, EvalResult, EvalRun, Lead, TranscriptTurn
from app.enums import CallOutcome, LeadStatus, TurnRole


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------
class _BaseRepository:
    """Shared plumbing: ORM UPDATEs must refresh objects already in the session."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _apply(self, instance: Any, **values: Any) -> None:
        """Mutate through the ORM (not a bulk UPDATE) so the identity map stays true."""
        for key, value in values.items():
            setattr(instance, key, value)
        await self.session.flush()


class LeadRepository(_BaseRepository):
    async def get(self, lead_id: uuid.UUID) -> Lead | None:
        return await self.session.get(Lead, lead_id)

    async def get_by_phone(self, phone: str) -> Lead | None:
        result = await self.session.execute(select(Lead).where(Lead.phone == phone))
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Lead]:
        stmt = select(Lead).order_by(Lead.created_at.desc()).limit(limit).offset(offset)
        if status:
            stmt = stmt.where(Lead.status == status)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def create(self, **kwargs: Any) -> Lead:
        lead = Lead(**kwargs)
        self.session.add(lead)
        await self.session.flush()
        return lead

    async def upsert_by_phone(self, phone: str, **kwargs: Any) -> tuple[Lead, bool]:
        """Insert or update by phone. Returns `(lead, created)`."""
        existing = await self.get_by_phone(phone)
        if existing is None:
            return await self.create(phone=phone, **kwargs), True
        for key, value in kwargs.items():
            if value is not None and hasattr(existing, key):
                setattr(existing, key, value)
        await self.session.flush()
        return existing, False

    async def due_for_call(self, limit: int = 20, max_attempts: int = 3) -> Sequence[Lead]:
        """Leads eligible to be dialled right now (respects DNC + backoff)."""
        now = utcnow()
        stmt = (
            select(Lead)
            .where(
                Lead.do_not_call.is_(False),
                Lead.attempts < max_attempts,
                Lead.status.notin_(
                    [
                        LeadStatus.BOOKED.value,
                        LeadStatus.REJECTED.value,
                        LeadStatus.DO_NOT_CALL.value,
                        LeadStatus.CALLING.value,
                        LeadStatus.EXHAUSTED.value,
                    ]
                ),
                (Lead.next_attempt_at.is_(None)) | (Lead.next_attempt_at <= now),
            )
            .order_by(Lead.next_attempt_at.asc().nulls_first(), Lead.created_at.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def needing_research(self, limit: int = 50, cache_days: int = 30) -> Sequence[Lead]:
        cutoff = utcnow() - timedelta(days=cache_days)
        stmt = (
            select(Lead)
            .where(
                Lead.do_not_call.is_(False),
                (Lead.researched_at.is_(None)) | (Lead.researched_at < cutoff),
            )
            .order_by(Lead.created_at.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def set_research(
        self, lead_id: uuid.UUID, notes: str, website: str | None = None
    ) -> None:
        lead = await self.get(lead_id)
        if lead is None:
            return
        values: dict[str, Any] = {
            "research_notes": notes,
            "researched_at": utcnow(),
            "status": LeadStatus.RESEARCHED.value,
        }
        if website:
            values["website"] = website
        await self._apply(lead, **values)

    async def mark_status(self, lead_id: uuid.UUID, status: LeadStatus | str) -> None:
        lead = await self.get(lead_id)
        if lead is None:
            return
        await self._apply(lead, status=status.value if isinstance(status, LeadStatus) else status)

    async def schedule_retry(self, lead_id: uuid.UUID, when: datetime | None) -> None:
        """Queue the next attempt. `when=None` only clears the timer; the caller
        owns the resulting status (booked / rejected / exhausted)."""
        lead = await self.get(lead_id)
        if lead is None:
            return
        values: dict[str, Any] = {"next_attempt_at": when}
        if when is not None:
            values["status"] = LeadStatus.QUEUED.value
        await self._apply(lead, **values)

    async def register_attempt(self, lead_id: uuid.UUID) -> None:
        lead = await self.get(lead_id)
        if lead is None:
            return
        await self._apply(
            lead,
            attempts=(lead.attempts or 0) + 1,
            last_attempt_at=utcnow(),
            status=LeadStatus.CALLING.value,
        )

    async def mark_do_not_call(self, lead_id: uuid.UUID) -> None:
        lead = await self.get(lead_id)
        if lead is None:
            return
        await self._apply(
            lead, do_not_call=True, status=LeadStatus.DO_NOT_CALL.value, next_attempt_at=None
        )

    async def count(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(Lead))
        return int(result.scalar_one())


# ---------------------------------------------------------------------------
# Calls + transcript
# ---------------------------------------------------------------------------
class CallRepository(_BaseRepository):
    async def get(self, call_id: uuid.UUID) -> Call | None:
        return await self.session.get(Call, call_id)

    async def get_by_sid(self, sid: str) -> Call | None:
        result = await self.session.execute(select(Call).where(Call.provider_call_sid == sid))
        return result.scalar_one_or_none()

    async def create(self, lead_id: uuid.UUID, **kwargs: Any) -> Call:
        call = Call(lead_id=lead_id, **kwargs)
        self.session.add(call)
        await self.session.flush()
        return call

    async def list(
        self, *, outcome: str | None = None, limit: int = 50, offset: int = 0
    ) -> Sequence[Call]:
        stmt = select(Call).order_by(Call.started_at.desc()).limit(limit).offset(offset)
        if outcome:
            stmt = stmt.where(Call.outcome == outcome)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def attach_sid(self, call_id: uuid.UUID, sid: str) -> None:
        call = await self.get(call_id)
        if call is None:
            return
        await self._apply(call, provider_call_sid=sid)

    async def mark_answered(self, call_id: uuid.UUID, answered_by: str | None = None) -> None:
        call = await self.get(call_id)
        if call is None:
            return
        await self._apply(call, answered_at=utcnow(), answered_by=answered_by)

    async def finalize(
        self,
        call_id: uuid.UUID,
        *,
        outcome: CallOutcome | str,
        reason: str | None = None,
        final_state: str | None = None,
        meeting_at: datetime | None = None,
        meeting_contact: str | None = None,
        objection_types: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        call = await self.session.get(Call, call_id)
        if call is None:
            return
        ended = utcnow()
        started = call.started_at or ended
        call.outcome = outcome.value if isinstance(outcome, CallOutcome) else outcome
        call.outcome_reason = reason
        call.final_state = final_state
        call.ended_at = ended
        call.duration_seconds = max((ended - started).total_seconds(), 0.0)
        if meeting_at:
            call.meeting_at = meeting_at
        if meeting_contact:
            call.meeting_contact = meeting_contact
        if objection_types:
            call.objection_types = objection_types
        if error:
            call.error = error
        await self.session.flush()

    async def add_turn(
        self,
        call_id: uuid.UUID,
        *,
        role: TurnRole | str,
        content: str,
        state: str | None = None,
        tool_called: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_valid: bool | None = None,
        asr_confidence: float | None = None,
        latency_ms: int | None = None,
        turn_index: int | None = None,
    ) -> TranscriptTurn:
        if turn_index is None:
            result = await self.session.execute(
                select(func.coalesce(func.max(TranscriptTurn.turn_index), -1)).where(
                    TranscriptTurn.call_id == call_id
                )
            )
            turn_index = int(result.scalar_one()) + 1
        turn = TranscriptTurn(
            call_id=call_id,
            turn_index=turn_index,
            role=role.value if isinstance(role, TurnRole) else role,
            content=content,
            state=state,
            tool_called=tool_called,
            tool_args=tool_args,
            tool_valid=tool_valid,
            asr_confidence=asr_confidence,
            latency_ms=latency_ms,
        )
        self.session.add(turn)
        await self.session.flush()
        return turn

    async def transcript(self, call_id: uuid.UUID) -> Sequence[TranscriptTurn]:
        result = await self.session.execute(
            select(TranscriptTurn)
            .where(TranscriptTurn.call_id == call_id)
            .order_by(TranscriptTurn.turn_index)
        )
        return result.scalars().all()

    async def attempt_number(self, lead_id: uuid.UUID) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Call).where(Call.lead_id == lead_id)
        )
        return int(result.scalar_one()) + 1


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------
class CostRepository(_BaseRepository):
    async def upsert(self, call_id: uuid.UUID, payload: dict[str, Any]) -> CostLog:
        result = await self.session.execute(select(CostLog).where(CostLog.call_id == call_id))
        log = result.scalar_one_or_none()
        if log is None:
            log = CostLog(call_id=call_id, **payload)
            self.session.add(log)
        else:
            for key, value in payload.items():
                setattr(log, key, value)
        await self.session.flush()
        return log

    async def get(self, call_id: uuid.UUID) -> CostLog | None:
        result = await self.session.execute(select(CostLog).where(CostLog.call_id == call_id))
        return result.scalar_one_or_none()

    async def summary(self, since: datetime | None = None) -> dict[str, Any]:
        """Aggregate cost/outcome report — Phase 5 exit criteria."""
        cost_stmt = select(
            func.count(CostLog.id),
            func.coalesce(func.sum(CostLog.cost_usd), 0),
            func.coalesce(func.sum(CostLog.stt_seconds), 0),
            func.coalesce(func.sum(CostLog.llm_input_tokens), 0),
            func.coalesce(func.sum(CostLog.llm_output_tokens), 0),
            func.coalesce(func.sum(CostLog.tts_characters), 0),
        ).join(Call, Call.id == CostLog.call_id)
        outcome_stmt = select(Call.outcome, func.count(Call.id)).group_by(Call.outcome)
        duration_stmt = select(func.coalesce(func.avg(Call.duration_seconds), 0))
        if since:
            cost_stmt = cost_stmt.where(Call.started_at >= since)
            outcome_stmt = outcome_stmt.where(Call.started_at >= since)
            duration_stmt = duration_stmt.where(Call.started_at >= since)

        calls, total_cost, stt_s, in_tok, out_tok, tts_chars = (
            await self.session.execute(cost_stmt)
        ).one()
        outcomes = {row[0]: row[1] for row in (await self.session.execute(outcome_stmt)).all()}
        avg_duration = float((await self.session.execute(duration_stmt)).scalar_one() or 0.0)

        calls = int(calls or 0)
        total_cost = float(total_cost or 0.0)
        booked = int(outcomes.get(CallOutcome.MEETING_BOOKED.value, 0))
        return {
            "calls_costed": calls,
            "total_cost_usd": round(total_cost, 6),
            "avg_cost_per_call_usd": round(total_cost / calls, 6) if calls else 0.0,
            "meetings_booked": booked,
            "cost_per_booked_meeting_usd": round(total_cost / booked, 6) if booked else None,
            "avg_call_duration_seconds": round(avg_duration, 2),
            "totals": {
                "stt_seconds": float(stt_s or 0),
                "llm_input_tokens": int(in_tok or 0),
                "llm_output_tokens": int(out_tok or 0),
                "tts_characters": int(tts_chars or 0),
            },
            "outcomes": outcomes,
        }


# ---------------------------------------------------------------------------
# Eval
# ---------------------------------------------------------------------------
class EvalRepository(_BaseRepository):
    async def create_run(self, **kwargs: Any) -> EvalRun:
        run = EvalRun(**kwargs)
        self.session.add(run)
        await self.session.flush()
        return run

    async def add_result(self, run_id: uuid.UUID | None, **kwargs: Any) -> EvalResult:
        result = EvalResult(run_id=run_id, **kwargs)
        self.session.add(result)
        await self.session.flush()
        return result

    async def latest_runs(self, limit: int = 10) -> Sequence[EvalRun]:
        result = await self.session.execute(
            select(EvalRun).order_by(EvalRun.created_at.desc()).limit(limit)
        )
        return result.scalars().all()
