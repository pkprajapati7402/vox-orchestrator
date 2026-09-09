"""Call endpoints: dial, inspect transcripts, run a campaign, read cost reports."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    CallDetail,
    CallOut,
    CampaignRequest,
    CostOut,
    DialRequest,
    DialResponse,
    SummaryResponse,
    TurnOut,
)
from app.db.base import get_session
from app.db.repositories import CallRepository, CostRepository
from app.services.call_service import CallRejected, CallService
from app.services.campaign import CampaignRunner
from app.services.reporting import build_summary

router = APIRouter(tags=["calls"])


@router.post("/calls/dial", response_model=DialResponse, summary="Dial a single lead")
async def dial(payload: DialRequest, session: AsyncSession = Depends(get_session)) -> DialResponse:
    service = CallService(session)
    try:
        result = await service.dial(payload.lead_id, dry_run=payload.dry_run)
    except CallRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return DialResponse(**result.as_dict())


@router.post("/campaigns/run", response_model=dict, summary="Dial every lead that is due")
async def run_campaign(payload: CampaignRequest) -> dict[str, object]:
    runner = CampaignRunner(concurrency=payload.concurrency, dry_run=payload.dry_run)
    report = await runner.run(limit=payload.limit)
    return report.as_dict()


@router.get("/calls", response_model=list[CallOut], summary="List calls")
async def list_calls(
    outcome: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[CallOut]:
    calls = await CallRepository(session).list(outcome=outcome, limit=limit, offset=offset)
    return [CallOut.model_validate(call) for call in calls]


@router.get("/calls/{call_id}", response_model=CallDetail, summary="Call detail + transcript")
async def get_call(call_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> CallDetail:
    repo = CallRepository(session)
    call = await repo.get(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    turns = await repo.transcript(call_id)
    cost = await CostRepository(session).get(call_id)
    detail = CallDetail.model_validate(call)
    detail.turns = [TurnOut.model_validate(turn) for turn in turns]
    detail.cost = CostOut.model_validate(cost) if cost else None
    return detail


@router.get("/reports/costs", response_model=SummaryResponse, summary="Cost & outcome summary")
async def cost_summary(
    days: int | None = Query(default=7, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
) -> SummaryResponse:
    summary = await build_summary(session, days=days)
    return SummaryResponse(**summary)
