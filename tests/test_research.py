"""Research tool: HTML extraction, note composition and caching behaviour."""

from __future__ import annotations

import httpx
import pytest

from app.llm.base import LLMClient, LLMError, LLMResponse, Usage
from app.research.scraper import BusinessScraper, discover_website, normalise_url
from app.research.service import ResearchService

SAMPLE_HTML = """
<html>
  <head>
    <title>Glow Studio Salon — Hair &amp; Bridal Studio in Hauz Khas</title>
    <meta name="description" content="Unisex salon in Hauz Khas offering hair, skin and bridal packages since 2014." />
  </head>
  <body>
    <script>var tracking = 1;</script>
    <h1>Bridal packages booking now</h1>
    <p>We are a family run salon established in 2014, specialising in bridal makeovers for South Delhi clients.</p>
  </body>
</html>
"""


class StubLLM(LLMClient):
    provider = "stub"
    model = "stub"

    def __init__(self, text: str = "", fail: bool = False) -> None:
        self.text = text
        self.fail = fail

    async def complete(self, messages, tools=None, **kwargs):  # noqa: ANN001, ANN003
        if self.fail:
            raise LLMError("down", provider=self.provider)
        return LLMResponse(text=self.text, usage=Usage(50, 20), provider=self.provider)


def test_normalise_url_adds_a_scheme():
    assert normalise_url("example.com") == "https://example.com"
    assert normalise_url("https://example.com") == "https://example.com"
    assert normalise_url("") is None
    assert normalise_url(None) is None


def test_discover_website_slugifies_the_business_name():
    assert discover_website("Glow Studio Salon").startswith("https://glowstudiosalon.com")
    assert discover_website("A&B") is None


def test_parse_extracts_title_description_and_highlights():
    result = BusinessScraper.parse(SAMPLE_HTML, "https://example.com")
    assert result.ok
    assert "Glow Studio Salon" in result.title
    assert "bridal packages" in result.description.lower()
    assert any("family run salon" in highlight.lower() for highlight in result.highlights)
    assert all("tracking" not in highlight for highlight in result.highlights)


async def test_scrape_handles_http_errors_gracefully():
    transport = httpx.MockTransport(lambda request: httpx.Response(404, text="nope"))
    scraper = BusinessScraper(client=httpx.AsyncClient(transport=transport))
    result = await scraper.scrape("https://example.com")
    assert not result.ok
    assert "404" in result.error


async def test_scrape_handles_network_failure():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    scraper = BusinessScraper(client=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    result = await scraper.scrape("https://example.com")
    assert not result.ok and "fetch failed" in result.error


async def test_research_service_builds_a_deterministic_note():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=SAMPLE_HTML))
    scraper = BusinessScraper(client=httpx.AsyncClient(transport=transport))
    service = ResearchService(scraper=scraper, use_llm=False)
    note = await service.research_lead(
        business_name="Glow Studio Salon",
        category="Salon",
        address="Hauz Khas, New Delhi",
        website="https://example.com",
    )
    assert note.usable
    assert "Glow Studio Salon is a salon in Hauz Khas" in note.text
    assert not note.llm_polished


async def test_research_service_uses_the_llm_when_available():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=SAMPLE_HTML))
    scraper = BusinessScraper(client=httpx.AsyncClient(transport=transport))
    service = ResearchService(
        scraper=scraper, llm=StubLLM("A Hauz Khas salon known for bridal packages since 2014.")
    )
    note = await service.research_lead(
        business_name="Glow Studio Salon", category="Salon", website="https://example.com"
    )
    assert note.llm_polished
    assert "bridal" in note.text.lower()


async def test_research_service_falls_back_when_the_llm_fails():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=SAMPLE_HTML))
    scraper = BusinessScraper(client=httpx.AsyncClient(transport=transport))
    service = ResearchService(scraper=scraper, llm=StubLLM(fail=True))
    note = await service.research_lead(
        business_name="Glow Studio Salon", website="https://example.com"
    )
    assert note.usable and not note.llm_polished


async def test_llm_refusal_does_not_produce_fabricated_context():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=SAMPLE_HTML))
    scraper = BusinessScraper(client=httpx.AsyncClient(transport=transport))
    service = ResearchService(scraper=scraper, llm=StubLLM("NO_USABLE_CONTEXT"))
    note = await service.research_lead(
        business_name="Glow Studio Salon", website="https://example.com"
    )
    assert "NO_USABLE_CONTEXT" not in note.text
    assert not note.llm_polished


async def test_missing_website_still_yields_a_safe_note():
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text=""))
    scraper = BusinessScraper(client=httpx.AsyncClient(transport=transport))
    service = ResearchService(scraper=scraper, use_llm=False)
    note = await service.research_lead(business_name="Mystery Shop", category="Shop")
    assert "do not reference their site" in note.text


@pytest.mark.usefixtures("db")
async def test_research_notes_are_cached_on_the_lead():
    from app.db.base import session_scope
    from app.db.repositories import LeadRepository

    async with session_scope() as session:
        repo = LeadRepository(session)
        lead = await repo.create(business_name="Glow", phone="+911140001001")
        await repo.set_research(lead.id, "Rated 4.6 with 320 reviews.", "https://example.com")
        refreshed = await repo.get(lead.id)
        assert refreshed.research_notes.startswith("Rated 4.6")
        assert refreshed.researched_at is not None
        assert await repo.needing_research() == []
