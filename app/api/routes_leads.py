"""Lead CRUD, bulk import and the research trigger."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import LeadCreate, LeadOut, ResearchRequest
from app.config import get_settings
from app.db.base import get_session
from app.db.repositories import LeadRepository
from app.llm.router import get_router
from app.logging_config import get_logger
from app.research.service import ResearchService

router = APIRouter(prefix="/leads", tags=["leads"])
log = get_logger(__name__)


@router.get("", response_model=list[LeadOut], summary="List leads")
async def list_leads(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[LeadOut]:
    leads = await LeadRepository(session).list(status=status_filter, limit=limit, offset=offset)
    return [LeadOut.model_validate(lead) for lead in leads]


@router.post(
    "", response_model=LeadOut, status_code=status.HTTP_201_CREATED, summary="Create a lead"
)
async def create_lead(payload: LeadCreate, session: AsyncSession = Depends(get_session)) -> LeadOut:
    repo = LeadRepository(session)
    lead, created = await repo.upsert_by_phone(
        payload.phone, **payload.model_dump(exclude={"phone"})
    )
    if not created:
        log.info("lead.updated", phone=payload.phone)
    return LeadOut.model_validate(lead)


@router.post(
    "/bulk", response_model=dict, status_code=status.HTTP_201_CREATED, summary="Bulk upsert leads"
)
async def bulk_leads(
    payload: list[LeadCreate], session: AsyncSession = Depends(get_session)
) -> dict[str, int]:
    repo = LeadRepository(session)
    created_count = 0
    for item in payload:
        _, created = await repo.upsert_by_phone(item.phone, **item.model_dump(exclude={"phone"}))
        created_count += int(created)
    return {
        "received": len(payload),
        "created": created_count,
        "updated": len(payload) - created_count,
    }


@router.get("/{lead_id}", response_model=LeadOut, summary="Get a lead")
async def get_lead(lead_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> LeadOut:
    lead = await LeadRepository(session).get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    return LeadOut.model_validate(lead)


@router.post("/{lead_id}/do-not-call", response_model=LeadOut, summary="Flag a lead as DNC")
async def mark_dnc(lead_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> LeadOut:
    repo = LeadRepository(session)
    lead = await repo.get(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    await repo.mark_do_not_call(lead_id)
    await session.flush()
    refreshed = await repo.get(lead_id)
    return LeadOut.model_validate(refreshed)


@router.post("/research", response_model=dict, summary="Research leads missing recent notes")
async def research_leads(
    payload: ResearchRequest, session: AsyncSession = Depends(get_session)
) -> dict[str, object]:
    settings = get_settings()
    repo = LeadRepository(session)
    cache_days = 0 if payload.force else settings.research_cache_days
    leads = await repo.needing_research(limit=payload.limit, cache_days=cache_days)

    service = ResearchService(llm=get_router())
    results = []
    try:
        for lead in leads:
            note = await service.research_lead(
                business_name=lead.business_name,
                category=lead.category,
                address=lead.address,
                website=lead.website,
            )
            if note.usable:
                await repo.set_research(lead.id, note.text, note.source_url)
            results.append(
                {
                    "lead_id": str(lead.id),
                    "business_name": lead.business_name,
                    "notes": note.text,
                    "source": note.source_url,
                    "llm_polished": note.llm_polished,
                }
            )
    finally:
        await service.aclose()
    return {"researched": len(results), "results": results}
