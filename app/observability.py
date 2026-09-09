"""Optional tracing (Arize Phoenix / OpenTelemetry).

Enabled with `TRACING_ENABLED=true` and the `tracing` extra installed. When the
packages are missing the helpers degrade to no-ops so nothing else has to care.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)

_tracer: Any | None = None
_initialised = False


def setup_tracing() -> bool:
    """Configure the OTLP exporter; returns True when tracing is live."""
    global _tracer, _initialised
    if _initialised:
        return _tracer is not None
    _initialised = True

    settings = get_settings()
    if not settings.tracing_enabled:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("tracing.disabled", reason='install the extra: pip install -e ".[tracing]"')
        return False

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.phoenix_collector_endpoint))
    )
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(settings.otel_service_name)
    log.info("tracing.enabled", endpoint=settings.phoenix_collector_endpoint)
    return True


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """Start a span if tracing is on; otherwise do nothing."""
    if _tracer is None:
        yield
        return
    with _tracer.start_as_current_span(name) as current:  # pragma: no cover - needs the extra
        for key, value in attributes.items():
            current.set_attribute(key, value)
        yield


def shutdown_tracing() -> None:  # pragma: no cover - process teardown
    global _tracer, _initialised
    _tracer = None
    _initialised = False


__all__ = ["setup_tracing", "shutdown_tracing", "span"]
