"""Live audio pipeline (Twilio Media Streams <-> Pipecat <-> our engine).

Design note
-----------
Pipecat owns the *audio*: VAD, the Twilio frame serializer, streaming STT and
streaming TTS. `ConversationEngine` owns the *decisions*: state, tools,
validation, guards, cost. The bridge below is deliberately thin — one processor
that hands each final transcript to the engine and pushes the engine's reply
back as TTS — because that is what keeps the offline eval harness testing the
same brain that runs on a real call.

`pipecat-ai` is an optional extra (`pip install -e ".[voice]"`). Everything here
imports it lazily so the API, the CLI, the eval harness and CI work without it.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.costs.tracker import CostAccumulator
from app.enums import CallOutcome
from app.llm.router import get_router
from app.logging_config import get_logger
from app.pipeline.engine import ConversationEngine, LeadContext

log = get_logger(__name__)


def pipecat_available() -> bool:
    """True when the optional voice extra is installed."""
    try:  # pragma: no cover - depends on optional extra
        import pipecat  # noqa: F401
        import pipecat_flows  # noqa: F401
    except Exception:
        return False
    return True


class PipecatNotInstalled(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            'pipecat-ai is not installed. Install the voice extra: pip install -e ".[voice]"'
        )


@dataclass
class CallSession:
    """Everything one live call needs, wired together and ready to run."""

    call_id: uuid.UUID
    lead: LeadContext
    engine: ConversationEngine
    cost: CostAccumulator

    @property
    def outcome(self) -> CallOutcome | None:
        return self.engine.outcome


def build_session(
    call_id: uuid.UUID,
    lead: LeadContext,
    *,
    sink: Any | None = None,
) -> CallSession:
    """Create the engine + cost accumulator for a call (no audio involved)."""
    cost = CostAccumulator()
    engine = ConversationEngine(lead=lead, llm=get_router(), cost=cost, sink=sink, call_id=call_id)
    return CallSession(call_id=call_id, lead=lead, engine=engine, cost=cost)


async def run_twilio_pipeline(  # pragma: no cover - requires the voice extra + a live socket
    websocket: Any,
    session: CallSession,
    *,
    stream_sid: str | None = None,
    call_sid: str | None = None,
) -> CallSession:
    """Run one call end to end over a Twilio Media Streams websocket."""
    if not pipecat_available():
        raise PipecatNotInstalled()

    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.frames.frames import EndFrame, TranscriptionFrame, TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.serializers.twilio import TwilioFrameSerializer
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
    from pipecat.services.groq.stt import GroqSTTService
    from pipecat.transports.network.fastapi_websocket import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    settings = get_settings()
    engine = session.engine

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid or "",
        call_sid=call_sid,
        account_sid=settings.twilio_account_sid or None,
        auth_token=settings.twilio_auth_token or None,
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_analyzer=SileroVADAnalyzer(),
            serializer=serializer,
        ),
    )
    stt = GroqSTTService(api_key=settings.groq_api_key, model=settings.groq_stt_model)
    tts = ElevenLabsTTSService(
        api_key=settings.elevenlabs_api_key,
        voice_id=settings.elevenlabs_voice_id,
        model=settings.elevenlabs_model,
        sample_rate=8000,
    )

    class EngineBridge(FrameProcessor):
        """Feeds final transcripts to the engine and speaks its replies."""

        def __init__(self) -> None:
            super().__init__()
            self._silence_task: asyncio.Task | None = None

        async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)

            if isinstance(frame, TranscriptionFrame) and frame.text:
                self._cancel_silence()
                confidence = getattr(frame, "confidence", None)
                session.cost.add_stt(
                    getattr(frame, "duration", 0.0) or 0.0, settings.groq_stt_model
                )
                turn = await engine.handle_user(frame.text, confidence)
                if turn.text:
                    await self.push_frame(TTSSpeakFrame(turn.text))
                if turn.ended:
                    await self.push_frame(EndFrame(), FrameDirection.DOWNSTREAM)
                else:
                    self._arm_silence()
                return

            await self.push_frame(frame, direction)

        def _arm_silence(self) -> None:
            self._cancel_silence()
            self._silence_task = asyncio.create_task(self._on_silence())

        def _cancel_silence(self) -> None:
            if self._silence_task and not self._silence_task.done():
                self._silence_task.cancel()
            self._silence_task = None

        async def _on_silence(self) -> None:
            try:
                await asyncio.sleep(settings.silence_timeout_seconds)
            except asyncio.CancelledError:
                return
            turn = await engine.handle_silence()
            if turn.text:
                await self.push_frame(TTSSpeakFrame(turn.text))
            if turn.ended:
                await self.push_frame(EndFrame(), FrameDirection.DOWNSTREAM)

    bridge = EngineBridge()
    pipeline = Pipeline([transport.input(), stt, bridge, tts, transport.output()])
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True, audio_in_sample_rate=8000, audio_out_sample_rate=8000
        ),
    )

    @transport.event_handler("on_client_connected")
    async def _on_connected(_transport: Any, _client: Any) -> None:
        opening = await engine.start()
        await task.queue_frames([TTSSpeakFrame(opening.text)])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_transport: Any, _client: Any) -> None:
        if not engine.ended:
            await engine.abort(CallOutcome.HUNG_UP, "caller disconnected")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    except Exception as exc:  # noqa: BLE001
        log.error("pipeline.crashed", call_id=str(session.call_id), error=str(exc)[:300])
        if not engine.ended:
            await engine.abort(CallOutcome.FAILED, f"pipeline error: {exc}")
    finally:
        if not engine.ended:
            await engine.abort(CallOutcome.HUNG_UP, "pipeline finished without a wrap-up")
    return session


__all__ = [
    "CallSession",
    "PipecatNotInstalled",
    "build_session",
    "pipecat_available",
    "run_twilio_pipeline",
]
