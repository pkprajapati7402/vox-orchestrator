"""Pre-call business research package."""

from app.research.scraper import BusinessScraper, ScrapeResult, discover_website, normalise_url
from app.research.service import ResearchNote, ResearchService

__all__ = [
    "BusinessScraper",
    "ResearchNote",
    "ResearchService",
    "ScrapeResult",
    "discover_website",
    "normalise_url",
]
