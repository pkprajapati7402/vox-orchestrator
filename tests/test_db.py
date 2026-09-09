"""Database layer: models, repositories, cost aggregation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.costs.tracker import CostAccumulator
from app.db.base import session_scope
from app.db.repositories import CallRepository, CostRepository, EvalRepository, LeadRepository
from app.enums import CallOutcome, LeadStatus, TurnRole

pytestmark = pytest.mark.usefixtures("db")


async def _make_lead(session, phone="+911140001001", **kwargs):  # noqa: ANN001, ANN003
    repo = LeadRepository(session)
    return await repo.create(
        business_name=kwargs.pop("business_name", "Glow Studio Salon"),
        phone=phone,
        category=kwargs.pop("category", "salon"),
        address=kwargs.pop("address", "Hauz Khas"),
        **kwargs,
    )


async def test_lead_upsert_is_idempotent_by_phone():
    async with session_scope() as session:
        repo = LeadRepository(session)
        lead, created = await repo.upsert_by_phone("+911140001001", business_name="Cafe A")
        assert created
        same, created_again = await repo.upsert_by_phone("+911140001001", business_name="Cafe B")
        assert not created_again
        assert same.id == lead.id
        assert same.business_name == "Cafe B"
        assert await repo.count() == 1


async def test_due_for_call_respects_dnc_and_backoff():
    async with session_scope() as session:
        repo = LeadRepository(session)
        ok = await _make_lead(session, "+911140001001")
        blocked = await _make_lead(session, "+911140001002", business_name="Blocked")
        later = await _make_lead(session, "+911140001003", business_name="Later")
        await repo.mark_do_not_call(blocked.id)
        await repo.schedule_retry(later.id, datetime.now(UTC) + timedelta(hours=2))

        due = await repo.due_for_call()
        ids = {lead.id for lead in due}
        assert ok.id in ids
        assert blocked.id not in ids
        assert later.id not in ids


async def test_register_attempt_increments_and_marks_calling():
    async with session_scope() as session:
        repo = LeadRepository(session)
        lead = await _make_lead(session)
        await repo.register_attempt(lead.id)
        await session.flush()
        refreshed = await repo.get(lead.id)
        assert refreshed.attempts == 1
        assert refreshed.status == LeadStatus.CALLING.value
        assert refreshed.last_attempt_at is not None


async def test_needing_research_filters_recent_notes():
    async with session_scope() as session:
        repo = LeadRepository(session)
        fresh = await _make_lead(session, "+911140001001")
        stale = await _make_lead(session, "+911140001002", business_name="Stale")
        await repo.set_research(fresh.id, "Rated 4.6", "https://example.com")
        await session.flush()
        pending = await repo.needing_research()
        assert {lead.id for lead in pending} == {stale.id}
        assert await repo.needing_research(cache_days=0) != []


async def test_transcript_turns_are_ordered_and_indexed():
    async with session_scope() as session:
        lead = await _make_lead(session)
        calls = CallRepository(session)
        call = await calls.create(lead.id)
        await calls.add_turn(call.id, role=TurnRole.AGENT, content="Hi", state="greeting")
        await calls.add_turn(call.id, role=TurnRole.LEAD, content="Yes", state="confirm_person")
        await calls.add_turn(
            call.id,
            role=TurnRole.AGENT,
            content="Great",
            state="confirm_person",
            tool_called="confirm_person",
            tool_args={"is_correct_person": True},
            tool_valid=True,
        )
        turns = await calls.transcript(call.id)
        assert [t.turn_index for t in turns] == [0, 1, 2]
        assert turns[2].tool_called == "confirm_person"
        assert turns[2].tool_args["is_correct_person"] is True


async def test_finalize_sets_duration_and_outcome():
    async with session_scope() as session:
        lead = await _make_lead(session)
        calls = CallRepository(session)
        call = await calls.create(lead.id)
        await calls.finalize(
            call.id,
            outcome=CallOutcome.MEETING_BOOKED,
            reason="booked",
            final_state="wrapup",
            meeting_at=datetime.now(UTC) + timedelta(days=1),
            objection_types=["price"],
        )
        refreshed = await calls.get(call.id)
        assert refreshed.outcome == CallOutcome.MEETING_BOOKED.value
        assert refreshed.ended_at is not None
        assert refreshed.duration_seconds >= 0
        assert refreshed.objection_types == ["price"]


async def test_cost_summary_reports_cost_per_meeting():
    async with session_scope() as session:
        lead = await _make_lead(session)
        calls = CallRepository(session)
        costs = CostRepository(session)

        booked = await calls.create(lead.id)
        await calls.finalize(booked.id, outcome=CallOutcome.MEETING_BOOKED)
        accumulator = CostAccumulator()
        accumulator.add_llm(10_000, 2_000, "llama-3.3-70b-versatile")
        accumulator.add_tts(800, "eleven_turbo_v2_5")
        await costs.upsert(booked.id, accumulator.to_cost_log_payload())

        rejected = await calls.create(lead.id)
        await calls.finalize(rejected.id, outcome=CallOutcome.NOT_INTERESTED)
        second = CostAccumulator()
        second.add_llm(5_000, 1_000, "llama-3.3-70b-versatile")
        await costs.upsert(rejected.id, second.to_cost_log_payload())

        summary = await costs.summary()
        assert summary["calls_costed"] == 2
        assert summary["meetings_booked"] == 1
        assert summary["total_cost_usd"] > 0
        assert summary["cost_per_booked_meeting_usd"] == summary["total_cost_usd"]
        assert summary["outcomes"][CallOutcome.MEETING_BOOKED.value] == 1


async def test_cost_upsert_is_idempotent():
    async with session_scope() as session:
        lead = await _make_lead(session)
        call = await CallRepository(session).create(lead.id)
        costs = CostRepository(session)
        await costs.upsert(call.id, {"cost_usd": 1})
        await costs.upsert(call.id, {"cost_usd": 2})
        log = await costs.get(call.id)
        assert float(log.cost_usd) == 2


async def test_eval_results_are_grouped_by_run():
    async with session_scope() as session:
        repo = EvalRepository(session)
        run = await repo.create_run(label="baseline", personas_run=2, task_completion_rate=1.0)
        await repo.add_result(
            run.id, persona="price_01", task_completed=True, turns_to_resolution=5
        )
        await repo.add_result(
            run.id, persona="rude_01", task_completed=False, turns_to_resolution=1
        )
        runs = await repo.latest_runs()
        assert runs[0].label == "baseline"
        assert len(runs[0].results) == 2
