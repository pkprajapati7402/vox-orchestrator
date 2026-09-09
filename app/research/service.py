"""Research service: turns raw scrape output into the 1-2 lines of context the
agent opens with, and caches it on `leads.research_notes` (Phase 3).

Two note builders:
  * deterministic — always available, composes facts from the lead row + scrape
  * LLM-polished  — optional single call that compresses those facts into one
    natural sentence; falls back to the deterministic note on any error

The prompt forbids inventing anything, and the deterministic note is what gets
stored if the model adds nothing useful — the agent must never open with a fact
we cannot source.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.llm.base import LLMClient, LLMError, Message
from app.logging_config import get_logger
from app.research.scraper import BusinessScraper, ScrapeResult, discover_website

log = get_logger(__name__)

_SUMMARY_SYSTEM = (
    "You compress business research into ONE short sentence a cold caller can open with. "
    "Use only the facts given. Never invent numbers, awards, clients, or claims. "
    "No greeting, no pitch, no more than 25 words. If the facts are too thin, reply exactly: "
    "NO_USABLE_CONTEXT"
)


@dataclass
class ResearchNote:
    text: str
    source_url: str | None = None
    llm_polished: bool = False
    facts: dict[str, Any] | None = None

    @property
    def usable(self) -> bool:
        return bool(self.text.strip())


class ResearchService:
    """Builds and caches per-lead research notes."""

    def __init__(
        self,
        scraper: BusinessScraper | None = None,
        llm: LLMClient | None = None,
        *,
        use_llm: bool = True,
    ) -> None:
        self.scraper = scraper or BusinessScraper()
        self.llm = llm
        self.use_llm = use_llm

    async def aclose(self) -> None:
        await self.scraper.aclose()

    async def research_lead(
        self,
        *,
        business_name: str,
        category: str | None = None,
        address: str | None = None,
        website: str | None = None,
    ) -> ResearchNote:
        settings = get_settings()
        if not settings.research_enabled:
            return ResearchNote(text=self._fallback_note(business_name, category, address))

        target = website or discover_website(business_name, address)
        scrape = await self.scraper.scrape(target)
        facts = self._collect_facts(business_name, category, address, scrape)
        deterministic = self._compose(facts)

        if self.use_llm and self.llm is not None and scrape.ok:
            polished = await self._polish(facts)
            if polished:
                return ResearchNote(
                    text=polished, source_url=scrape.url, llm_polished=True, facts=facts
                )
        return ResearchNote(text=deterministic, source_url=scrape.url, facts=facts)

    # --- internals -------------------------------------------------------
    @staticmethod
    def _collect_facts(
        business_name: str,
        category: str | None,
        address: str | None,
        scrape: ScrapeResult,
    ) -> dict[str, Any]:
        return {
            "business_name": business_name,
            "category": category,
            "area": (address or "").split(",")[0].strip() or None,
            "website_title": scrape.title,
            "website_description": scrape.description,
            "website_highlights": scrape.highlights,
            "scrape_error": scrape.error,
        }

    @staticmethod
    def _compose(facts: dict[str, Any]) -> str:
        bits: list[str] = []
        name = facts["business_name"]
        category = facts.get("category")
        area = facts.get("area")
        if category and area:
            bits.append(f"{name} is a {category.lower()} in {area}.")
        elif category:
            bits.append(f"{name} is a {category.lower()}.")
        elif area:
            bits.append(f"{name} operates in {area}.")
        else:
            bits.append(f"{name} is a local business.")

        description = facts.get("website_description") or facts.get("website_title")
        if description:
            bits.append(f'Their site says: "{description}".')
        highlights = facts.get("website_highlights") or []
        if highlights:
            bits.append(f'Site highlight: "{highlights[0]}".')
        if facts.get("scrape_error") and not description and not highlights:
            bits.append("No website context available — do not reference their site.")
        return " ".join(bits)

    async def _polish(self, facts: dict[str, Any]) -> str | None:
        assert self.llm is not None
        payload = "\n".join(
            f"- {key}: {value}" for key, value in facts.items() if value and key != "scrape_error"
        )
        try:
            response = await self.llm.complete(
                [
                    Message(role="system", content=_SUMMARY_SYSTEM),
                    Message(role="user", content=f"Facts:\n{payload}"),
                ],
                None,
                temperature=0.2,
                max_tokens=80,
            )
        except LLMError as exc:
            log.info("research.llm_unavailable", error=str(exc)[:160])
            return None
        text = (response.text or "").strip()
        if not text or "NO_USABLE_CONTEXT" in text.upper():
            return None
        return text[:400]

    @staticmethod
    def _fallback_note(business_name: str, category: str | None, address: str | None) -> str:
        area = (address or "").split(",")[0].strip()
        if category and area:
            return f"{business_name} is a {category.lower()} in {area}."
        return f"{business_name} is a local business."


__all__ = ["ResearchNote", "ResearchService"]
