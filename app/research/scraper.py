"""Lightweight business research scraper.

Pulls a couple of factual lines from the lead's own website — title, meta
description, an "about" sentence, opening hours — so the agent can open with
something specific instead of a generic script. Deliberately shallow: one page,
a hard timeout, no JS rendering, and a polite user agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)

_WS_RE = re.compile(r"\s+")
_ABOUT_HINTS = ("about", "we are", "we're", "our story", "specialis", "specializ", "established")
_MAX_HTML_BYTES = 400_000


@dataclass
class ScrapeResult:
    url: str | None = None
    title: str | None = None
    description: str | None = None
    highlights: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.title or self.description or self.highlights)


def _clean(text: str | None, limit: int = 220) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip()[:limit]


def normalise_url(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    return raw if parsed.netloc else None


class BusinessScraper:
    """Fetches and extracts a few lines of context from a business website."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self.timeout = settings.research_timeout_seconds
        self.user_agent = settings.research_user_agent
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> BusinessScraper:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def scrape(self, url: str | None) -> ScrapeResult:
        target = normalise_url(url)
        if not target:
            return ScrapeResult(error="no website on record")
        try:
            response = await self._http().get(target)
        except httpx.HTTPError as exc:
            log.info("research.fetch_failed", url=target, error=str(exc)[:160])
            return ScrapeResult(url=target, error=f"fetch failed: {exc}")
        if response.status_code >= 400:
            return ScrapeResult(url=target, error=f"http {response.status_code}")
        return self.parse(response.text[:_MAX_HTML_BYTES], str(response.url))

    @staticmethod
    def parse(html: str, url: str | None = None) -> ScrapeResult:
        """Extract title/description/highlights from raw HTML."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:  # pragma: no cover - dependency is declared
            return ScrapeResult(url=url, error="beautifulsoup4 not installed")

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        title = _clean(soup.title.string if soup.title and soup.title.string else None, 120)
        description = ""
        for selector in (
            {"name": "description"},
            {"property": "og:description"},
            {"name": "twitter:description"},
        ):
            meta = soup.find("meta", attrs=selector)
            if meta and meta.get("content"):
                description = _clean(meta["content"])
                break

        highlights: list[str] = []
        for heading in soup.find_all(["h1", "h2"], limit=6):
            text = _clean(heading.get_text(), 120)
            if text and text.lower() != title.lower():
                highlights.append(text)
        for paragraph in soup.find_all("p", limit=25):
            text = _clean(paragraph.get_text())
            lowered = text.lower()
            if len(text) > 60 and any(hint in lowered for hint in _ABOUT_HINTS):
                highlights.append(text)
                break

        # Deduplicate while preserving order.
        seen: set[str] = set()
        unique = [h for h in highlights if not (h.lower() in seen or seen.add(h.lower()))]
        return ScrapeResult(
            url=url, title=title or None, description=description or None, highlights=unique[:3]
        )


def discover_website(business_name: str, address: str | None = None) -> str | None:
    """Best-effort guess of a website when the lead list has none.

    Intentionally conservative: a plausible `.com` from the business name, which
    the scraper will simply fail on if it does not exist. (Swap in a Places API
    lookup here when a paid key is available.)
    """
    slug = re.sub(r"[^a-z0-9]+", "", business_name.lower())
    if len(slug) < 4:
        return None
    return urljoin(f"https://{slug}.com", "/")


__all__ = ["BusinessScraper", "ScrapeResult", "discover_website", "normalise_url"]
