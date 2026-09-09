"""Turn sinks: where transcript rows go while a call is in flight.

`DbTurnSink` writes each turn as it happens (Phase 5: "log every turn's
transcript and any tool calls"), using a short-lived session per turn so a slow
database can never stall the audio pipeline for longer than one insert.
`MemoryTurnSink` is the test/eval double, and `CompositeSink` lets tracing be
attached alongside persistence.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from app.db.base import session_scope
from app.db.repositories import CallRepository
from app.logging_config import get_logger
from app.pipeline.engine import TurnRecord

log = get_logger(__name__)


class MemoryTurnSink:
    """Collects turns in memory (tests, eval, dry runs)."""

    def __init__(self) -> None:
        self.records: list[TurnRecord] = []

    async def on_turn(self, record: TurnRecord) -> None:
        self.records.append(record)


class DbTurnSink:
    """Persists each turn to `transcript_turns`."""

    def __init__(self, call_id: uuid.UUID) -> None:
        self.call_id = call_id
        self._index = 0

    async def on_turn(self, record: TurnRecord) -> None:
        index = self._index
        self._index += 1
        try:
            async with session_scope() as session:
                await CallRepository(session).add_turn(
                    self.call_id,
                    role=record.role,
                    content=record.content,
                    state=record.state.value,
                    tool_called=record.tool_called,
                    tool_args=record.tool_args,
                    tool_valid=record.tool_valid,
                    asr_confidence=record.asr_confidence,
                    latency_ms=record.latency_ms,
                    turn_index=index,
                )
        except Exception as exc:  # noqa: BLE001 - never kill a live call over logging
            log.error(
                "persistence.turn_write_failed", call_id=str(self.call_id), error=str(exc)[:200]
            )


class CompositeSink:
    """Fans a turn out to several sinks; one failing sink cannot break the others."""

    def __init__(self, sinks: Iterable[object]) -> None:
        self.sinks = list(sinks)

    async def on_turn(self, record: TurnRecord) -> None:
        for sink in self.sinks:
            try:
                await sink.on_turn(record)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                log.error("persistence.sink_failed", sink=type(sink).__name__, error=str(exc)[:200])


__all__ = ["CompositeSink", "DbTurnSink", "MemoryTurnSink"]
