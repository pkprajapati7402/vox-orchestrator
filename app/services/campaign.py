"""Campaign runner: dial a batch of due leads, respecting the compliance gate,
the retry backoff and a concurrency cap.

The cap is not decoration — every concurrent call is a live Pipecat pipeline
plus an STT/LLM/TTS stream, and free tiers rate-limit hard.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.db.base import session_scope
from app.db.repositories import LeadRepository
from app.logging_config import get_logger
from app.services.call_service import CallRejected, CallService

log = get_logger(__name__)


@dataclass
class CampaignReport:
    requested: int = 0
    dialed: int = 0
    blocked: int = 0
    failed: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "dialed": self.dialed,
            "blocked": self.blocked,
            "failed": self.failed,
            "results": self.results,
        }


class CampaignRunner:
    """Dials the leads that are due, `concurrency` at a time."""

    def __init__(self, *, concurrency: int = 2, dry_run: bool = False) -> None:
        self.concurrency = max(1, concurrency)
        self.dry_run = dry_run
        self.settings = get_settings()

    async def run(self, limit: int = 10) -> CampaignReport:
        async with session_scope() as session:
            leads = await LeadRepository(session).due_for_call(
                limit=limit, max_attempts=self.settings.call_max_attempts
            )
            lead_ids = [(lead.id, lead.business_name, lead.phone) for lead in leads]

        report = CampaignReport(requested=len(lead_ids))
        semaphore = asyncio.Semaphore(self.concurrency)

        async def dial_one(lead_id: Any, name: str, phone: str) -> None:
            async with semaphore:
                try:
                    async with session_scope() as session:
                        service = CallService(session)
                        result = await service.dial(lead_id, dry_run=self.dry_run)
                    report.dialed += 1
                    report.results.append({"business": name, "phone": phone, **result.as_dict()})
                except CallRejected as exc:
                    report.blocked += 1
                    report.results.append(
                        {"business": name, "phone": phone, "status": "blocked", "reason": str(exc)}
                    )
                    log.info("campaign.lead_blocked", business=name, reason=str(exc)[:200])
                except Exception as exc:  # noqa: BLE001 - one bad lead must not kill the batch
                    report.failed += 1
                    report.results.append(
                        {"business": name, "phone": phone, "status": "error", "reason": str(exc)}
                    )
                    log.error("campaign.lead_failed", business=name, error=str(exc)[:300])

        await asyncio.gather(*(dial_one(*item) for item in lead_ids))
        log.info(
            "campaign.finished",
            requested=report.requested,
            dialed=report.dialed,
            blocked=report.blocked,
            failed=report.failed,
        )
        return report


__all__ = ["CampaignReport", "CampaignRunner"]
