"""Call orchestration: compliance gate, AMD branch, finalisation, retries."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.config import get_settings
from app.db.base import session_scope
from app.db.repositories import CallRepository, CostRepository, LeadRepository
from app.enums import CallOutcome, LeadStatus
from app.llm.mock import MockLLMClient
from app.pipeline.engine import ConversationEngine
from app.services.call_service import CallRejected, CallService
from app.services.campaign import CampaignRunner
from app.services.reporting import build_summary, render_summary_markdown
from app.telephony.twilio_client import PlacedCall, TwilioError

pytestmark = pytest.mark.usefixtures("db")


class FakeTelephony:
    """Stands in for Twilio; records what would have been dialled."""

    def __init__(self, *, configured: bool = True, fail: bool = False) -> None:
        self.configured = configured
        self.fail = fail
        self.placed: list[tuple[str, str]] = []
        self.hangups: list[str] = []

    def is_configured(self) -> bool:
        return self.configured

    def place_call(self, to: str, call_id: str) -> PlacedCall:
        if self.fail:
            raise TwilioError("twilio exploded")
        self.placed.append((to, call_id))
        return PlacedCall(
            sid=f"CA{len(self.placed):032d}", to=to, from_="+15005550006", status="queued"
        )

    def hangup(self, sid: str) -> None:
        self.hangups.append(sid)


async def _seed_lead(**kwargs):  # noqa: ANN003
    async with session_scope() as session:
        lead = await LeadRepository(session).create(
            business_name=kwargs.pop("business_name", "Glow Studio Salon"),
            phone=kwargs.pop("phone", "+911140001001"),
            category="salon",
            address="Hauz Khas",
            **kwargs,
        )
        return lead.id


async def test_dial_places_a_call_and_records_the_attempt():
    lead_id = await _seed_lead()
    telephony = FakeTelephony()
    async with session_scope() as session:
        result = await CallService(session, telephony=telephony).dial(lead_id)
    assert telephony.placed and telephony.placed[0][0] == "+911140001001"
    async with session_scope() as session:
        call = await CallRepository(session).get(result.call_id)
        lead = await LeadRepository(session).get(lead_id)
        assert call.provider_call_sid == result.provider_call_sid
        assert lead.attempts == 1
        assert lead.status == LeadStatus.CALLING.value


async def test_dry_run_does_not_dial():
    lead_id = await _seed_lead()
    telephony = FakeTelephony()
    async with session_scope() as session:
        result = await CallService(session, telephony=telephony).dial(lead_id, dry_run=True)
    assert result.dry_run and not telephony.placed


async def test_dnc_lead_is_never_dialled():
    lead_id = await _seed_lead(do_not_call=True)
    telephony = FakeTelephony()
    async with session_scope() as session:
        with pytest.raises(CallRejected):
            await CallService(session, telephony=telephony).dial(lead_id)
    assert not telephony.placed


async def test_max_attempts_blocks_further_dialling():
    lead_id = await _seed_lead()
    settings = get_settings()
    async with session_scope() as session:
        repo = LeadRepository(session)
        for _ in range(settings.call_max_attempts):
            await repo.register_attempt(lead_id)
    async with session_scope() as session:
        with pytest.raises(CallRejected):
            await CallService(session, telephony=FakeTelephony()).dial(lead_id)


async def test_twilio_failure_marks_the_call_failed_and_schedules_a_retry():
    lead_id = await _seed_lead()
    async with session_scope() as session:
        with pytest.raises(CallRejected):
            await CallService(session, telephony=FakeTelephony(fail=True)).dial(lead_id)
    async with session_scope() as session:
        lead = await LeadRepository(session).get(lead_id)
        calls = await CallRepository(session).list()
        assert calls[0].outcome == CallOutcome.FAILED.value
        assert lead.next_attempt_at is not None


async def test_amd_voicemail_hangs_up_logs_and_retries():
    lead_id = await _seed_lead()
    telephony = FakeTelephony()
    async with session_scope() as session:
        result = await CallService(session, telephony=telephony).dial(lead_id)
    async with session_scope() as session:
        payload = await CallService(session, telephony=telephony).handle_amd(
            result.call_id, "machine_end_beep"
        )
    assert payload["hung_up"] and payload["is_machine"]
    assert telephony.hangups == [result.provider_call_sid]
    async with session_scope() as session:
        call = await CallRepository(session).get(result.call_id)
        lead = await LeadRepository(session).get(lead_id)
        assert call.outcome == CallOutcome.VOICEMAIL.value
        assert lead.next_attempt_at is not None


async def test_amd_human_keeps_the_call_alive():
    lead_id = await _seed_lead()
    telephony = FakeTelephony()
    async with session_scope() as session:
        result = await CallService(session, telephony=telephony).dial(lead_id)
    async with session_scope() as session:
        payload = await CallService(session, telephony=telephony).handle_amd(
            result.call_id, "human"
        )
    assert not payload["hung_up"]
    async with session_scope() as session:
        call = await CallRepository(session).get(result.call_id)
        assert call.outcome == CallOutcome.IN_PROGRESS.value
        assert call.answered_by == "human"


async def test_status_callback_closes_unanswered_calls():
    lead_id = await _seed_lead()
    telephony = FakeTelephony()
    async with session_scope() as session:
        result = await CallService(session, telephony=telephony).dial(lead_id)
    async with session_scope() as session:
        await CallService(session, telephony=telephony).handle_status(
            result.call_id, "no-answer", 0
        )
    async with session_scope() as session:
        call = await CallRepository(session).get(result.call_id)
        assert call.outcome == CallOutcome.NO_ANSWER.value


async def test_finalize_from_engine_persists_outcome_and_cost():
    lead_id = await _seed_lead()
    telephony = FakeTelephony()
    async with session_scope() as session:
        service = CallService(session, telephony=telephony)
        result = await service.dial(lead_id)
        lead_ctx = await service.lead_context(lead_id)

    engine = ConversationEngine(lead_ctx, MockLLMClient(), call_id=result.call_id)
    await engine.start()
    for utterance in [
        "Yes, speaking.",
        "Walk-ins mostly.",
        "Okay, that sounds interesting. How does that work?",
        "Thursday at 4 works",
    ]:
        if engine.ended:
            break
        await engine.handle_user(utterance)

    async with session_scope() as session:
        metrics = await CallService(session, telephony=telephony).finalize_from_engine(
            result.call_id, engine
        )
    assert metrics["outcome"] == CallOutcome.MEETING_BOOKED.value

    async with session_scope() as session:
        call = await CallRepository(session).get(result.call_id)
        cost = await CostRepository(session).get(result.call_id)
        lead = await LeadRepository(session).get(lead_id)
        assert call.outcome == CallOutcome.MEETING_BOOKED.value
        assert call.meeting_at is not None
        assert float(cost.cost_usd) > 0
        assert lead.status == LeadStatus.BOOKED.value
        assert lead.next_attempt_at is None


async def test_not_interested_stops_the_retry_loop():
    lead_id = await _seed_lead()
    telephony = FakeTelephony()
    async with session_scope() as session:
        service = CallService(session, telephony=telephony)
        result = await service.dial(lead_id)
        lead_ctx = await service.lead_context(lead_id)

    engine = ConversationEngine(lead_ctx, MockLLMClient(), call_id=result.call_id)
    await engine.start()
    for utterance in ["Yes, speaking.", "Referrals.", "Please don't call again."]:
        if engine.ended:
            break
        await engine.handle_user(utterance)

    async with session_scope() as session:
        await CallService(session, telephony=telephony).finalize_from_engine(result.call_id, engine)
    async with session_scope() as session:
        lead = await LeadRepository(session).get(lead_id)
        assert lead.status == LeadStatus.REJECTED.value
        assert lead.next_attempt_at is None


async def test_campaign_runner_dials_due_leads_only(monkeypatch):
    await _seed_lead(phone="+911140001001")
    blocked = await _seed_lead(phone="+911140001002", business_name="Blocked", do_not_call=True)
    later = await _seed_lead(phone="+911140001003", business_name="Later")
    async with session_scope() as session:
        await LeadRepository(session).schedule_retry(later, datetime.now(UTC) + timedelta(hours=5))

    report = await CampaignRunner(concurrency=2, dry_run=True).run(limit=10)
    assert report.requested == 1
    assert report.dialed == 1
    assert str(blocked) not in str(report.results)


async def test_weekly_summary_renders_markdown():
    lead_id = await _seed_lead()
    telephony = FakeTelephony()
    async with session_scope() as session:
        service = CallService(session, telephony=telephony)
        result = await service.dial(lead_id)
        lead_ctx = await service.lead_context(lead_id)
    engine = ConversationEngine(lead_ctx, MockLLMClient(), call_id=result.call_id)
    await engine.start()
    await engine.handle_user("Yes, speaking.")
    await engine.abort(CallOutcome.HUNG_UP, "test")
    async with session_scope() as session:
        await CallService(session, telephony=telephony).finalize_from_engine(result.call_id, engine)

    async with session_scope() as session:
        summary = await build_summary(session, days=7)
    assert summary["total_calls"] == 1
    markdown = render_summary_markdown(summary)
    assert "Cost / booked meeting" in markdown
    assert "Outcomes" in markdown


async def test_dial_unknown_lead_is_rejected():
    async with session_scope() as session:
        with pytest.raises(CallRejected):
            await CallService(session, telephony=FakeTelephony()).dial(uuid.uuid4())
