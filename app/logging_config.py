"""Structured logging setup (structlog, human-readable in dev / JSON in prod)."""

from __future__ import annotations

import logging
import sys

import structlog

from app.config import get_settings

_CONFIGURED = False


def configure_logging(force: bool = False) -> None:
    """Configure stdlib logging + structlog once per process."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    settings = get_settings()
    level = getattr(logging, settings.log_level, logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    for noisy in ("httpx", "httpcore", "asyncio", "twilio.http_client"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger, configuring logging on first use."""
    configure_logging()
    return structlog.get_logger(name)  # type: ignore[return-value]
