"""Call orchestration — the layer that turns a lead row into a placed call and,
when the conversation is over, into a fully costed, fully logged record.

Everything that must happen exactly once per call lives here: the compliance
gate, the attempt counter, the Twilio dial, the AMD branch, cost persistence
and the retry-backoff decision.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.costs.tracker import CostAccumulator
from app.db.repositories import CallRepository, CostRepository, LeadRepository
from app.enums import CallOutcome, LeadStatus
from app.logging_config import get_logger
from app.pipeline.engine import ConversationEngine, LeadContext
from app.telephony.amd import classify_answered_by, next_attempt_at
from app.telephony.compliance import check_compliance
from app.telephony.twilio_client import TwilioError, TwilioTelephony, get_telephony

log = get_logger(__name__)


class CallRejected(RuntimeError):
    """Dialling was refused before any money was spent (compliance, config, state)."""


@dataclass
class DialResult:
    call_id: uuid.UUID
    lead_id: uuid.UUID
    phone: str
    provider_call_sid: str | None
    status: str
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": str(self.call_id),
            "lead_id": str(self.lead_id),
            "phone": self.phone,
            "provider_call_sid": self.provider_call_sid,
            "status": self.status,
            "dry_run": self.dry_run,
        }


class CallService:
    """Coordinates leads, calls, telephony and cost logs for one DB session."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        telephony: TwilioTelephony | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.telephony = telephony or get_telephony()
        self.leads = LeadRepository(session)
        self.calls = CallRepository(session)
        self.costs = CostRepository(session)

    # --- dialling ---------------------------------------------------------
    async def dial(self, lead_id: uuid.UUID, *, dry_run: bool = False) -> DialResult:
        """Compliance-check, create the call row, and place the outbound call."""
        lead = await self.leads.get(lead_id)
        if lead is None:
            raise CallRejected(f"lead {lead_id} not found")

        decision = check_compliance(
            do_not_call=lead.do_not_call, phone=lead.phone, settings=self.settings
        )
        if not decision.allowed:
            log.warning("call.blocked", lead_id=str(lead_id), reason=decision.reason)
            raise CallRejected(decision.reason)

        if lead.attempts >= self.settings.call_max_attempts:
            raise CallRejected(
                f"lead has already been attempted {lead.attempts} times "
                f"(CALL_MAX_ATTEMPTS={self.settings.call_max_attempts})"
            )

        attempt = await self.calls.attempt_number(lead_id)
        call = await self.calls.create(lead_id, attempt_number=attempt)
        await self.leads.register_attempt(lead_id)

        if dry_run:
            log.info("call.dry_run", call_id=str(call.id), phone=lead.phone)
            return DialResult(call.id, lead_id, lead.phone, None, "dry_run", dry_run=True)

        if not self.telephony.is_configured():
            await self.calls.finalize(
                call.id, outcome=CallOutcome.FAILED, reason="twilio not configured"
            )
            raise CallRejected("Twilio is not configured; set the TWILIO_* variables")

        try:
            placed = await asyncio.to_thread(self.telephony.place_call, lead.phone, str(call.id))
        except TwilioError as exc:
            await self.calls.finalize(
                call.id, outcome=CallOutcome.FAILED, reason=str(exc), error=str(exc)
            )
            await self._schedule_retry(lead_id, attempt, CallOutcome.FAILED)
            raise CallRejected(str(exc)) from exc

        await self.calls.attach_sid(call.id, placed.sid)
        return DialResult(call.id, lead_id, lead.phone, placed.sid, placed.status)

    # --- webhook handlers --------------------------------------------------
    async def handle_amd(self, call_id: uuid.UUID, answered_by: str | None) -> dict[str, Any]:
        """Apply the AMD decision: hang up + log voicemail, or carry on."""
        decision = classify_answered_by(answered_by)
        call = await self.calls.get(call_id)
        if call is None:
            return {"call_id": str(call_id), "known": False, **_amd_payload(decision)}

        await self.calls.mark_answered(call_id, decision.answered_by.value)
        if decision.should_hang_up:
            await self.calls.finalize(
                call_id,
                outcome=decision.outcome or CallOutcome.VOICEMAIL,
                reason=decision.reason,
                final_state="wrapup",
            )
            await self._schedule_retry(
                call.lead_id, call.attempt_number, decision.outcome or CallOutcome.VOICEMAIL
            )
            if call.provider_call_sid and self.telephony.is_configured():
                try:
                    await asyncio.to_thread(self.telephony.hangup, call.provider_call_sid)
                except TwilioError as exc:  # pragma: no cover - network dependent
                    log.warning("call.hangup_failed", call_id=str(call_id), error=str(exc)[:200])
        return {"call_id": str(call_id), "known": True, **_amd_payload(decision)}

    async def handle_status(
        self, call_id: uuid.UUID, status: str, duration_seconds: float | None = None
    ) -> dict[str, Any]:
        """Twilio status callback: close out calls that never reached a conversation."""
        call = await self.calls.get(call_id)
        if call is None:
            return {"call_id": str(call_id), "known": False, "status": status}

        status = (status or "").lower()
        terminal_map = {
            "no-answer": CallOutcome.NO_ANSWER,
            "busy": CallOutcome.BUSY,
            "failed": CallOutcome.FAILED,
            "canceled": CallOutcome.FAILED,
        }
        if status in terminal_map and call.ended_at is None:
            outcome = terminal_map[status]
            await self.calls.finalize(call_id, outcome=outcome, reason=f"twilio status: {status}")
            await self._schedule_retry(call.lead_id, call.attempt_number, outcome)
        elif status == "completed" and call.ended_at is None:
            # The websocket handler normally finalises; this is the safety net.
            await self.calls.finalize(
                call_id, outcome=CallOutcome.HUNG_UP, reason="call completed without a wrap-up"
            )
            await self._schedule_retry(call.lead_id, call.attempt_number, CallOutcome.HUNG_UP)

        if duration_seconds:
            cost = CostAccumulator()
            cost.add_telephony(duration_seconds)
            existing = await self.costs.get(call_id)
            if existing is None:
                await self.costs.upsert(call_id, cost.to_cost_log_payload())
        return {"call_id": str(call_id), "known": True, "status": status}

    # --- completion --------------------------------------------------------
    async def finalize_from_engine(
        self, call_id: uuid.UUID, engine: ConversationEngine
    ) -> dict[str, Any]:
        """Persist outcome, meeting, objections and the cost log for a finished call."""
        call = await self.calls.get(call_id)
        outcome = engine.outcome or CallOutcome.HUNG_UP
        await self.calls.finalize(
            call_id,
            outcome=outcome,
            reason=engine.outcome_reason,
            final_state=engine.state.value,
            meeting_at=engine.meeting_at,
            meeting_contact=engine.meeting_contact,
            objection_types=engine.objection_guard.types_seen or None,
        )
        await self.costs.upsert(call_id, engine.cost.to_cost_log_payload())

        if call is not None:
            await self._apply_outcome_to_lead(call.lead_id, call.attempt_number, outcome)
        metrics = engine.metrics()
        log.info(
            "call.finalized",
            call_id=str(call_id),
            outcome=outcome.value,
            turns=metrics["turns"],
            cost_usd=metrics["cost"]["cost_usd"],
        )
        return metrics

    async def _apply_outcome_to_lead(
        self, lead_id: uuid.UUID, attempt_number: int, outcome: CallOutcome
    ) -> None:
        if outcome is CallOutcome.MEETING_BOOKED:
            await self.leads.schedule_retry(lead_id, None)
            await self.leads.mark_status(lead_id, LeadStatus.BOOKED)
            return
        if outcome is CallOutcome.NOT_INTERESTED:
            await self.leads.schedule_retry(lead_id, None)
            await self.leads.mark_status(lead_id, LeadStatus.REJECTED)
            return
        if outcome is CallOutcome.WRONG_PERSON:
            await self.leads.mark_status(lead_id, LeadStatus.CONTACTED)
        await self._schedule_retry(lead_id, attempt_number, outcome)

    async def _schedule_retry(
        self, lead_id: uuid.UUID, attempt_number: int, outcome: CallOutcome
    ) -> None:
        """Apply the backoff schedule for retryable outcomes; otherwise stop."""
        if not outcome.is_retryable:
            await self.leads.mark_status(lead_id, LeadStatus.CONTACTED)
            await self.leads.schedule_retry(lead_id, None)
            return
        when = next_attempt_at(attempt_number)
        await self.leads.schedule_retry(lead_id, when)
        if when is None:
            # Backoff schedule exhausted: stop calling this lead.
            await self.leads.mark_status(lead_id, LeadStatus.EXHAUSTED)
        log.info(
            "call.retry_scheduled",
            lead_id=str(lead_id),
            attempt=attempt_number,
            next_attempt_at=when.isoformat() if when else None,
        )

    # --- helpers -----------------------------------------------------------
    async def lead_context(self, lead_id: uuid.UUID) -> LeadContext:
        lead = await self.leads.get(lead_id)
        if lead is None:
            raise CallRejected(f"lead {lead_id} not found")
        return LeadContext.from_model(lead)


def _amd_payload(decision: Any) -> dict[str, Any]:
    return {
        "answered_by": decision.answered_by.value,
        "is_machine": decision.is_machine,
        "hung_up": decision.should_hang_up,
        "reason": decision.reason,
    }


__all__ = ["CallRejected", "CallService", "DialResult"]
