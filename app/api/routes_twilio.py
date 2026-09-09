"""Twilio webhooks and the Media Streams websocket.

  POST /twilio/voice   -> TwiML that connects the call to our websocket
  POST /twilio/amd     -> async AMD result; voicemail hangs the call up
  POST /twilio/status  -> lifecycle callbacks (no-answer, busy, completed, ...)
  WS   /twilio/stream  -> bidirectional audio, handed to the Pipecat pipeline

Every HTTP webhook verifies `X-Twilio-Signature` unless verification is
explicitly disabled (`TWILIO_VALIDATE_SIGNATURES=false` for local curl testing).
"""

from __future__ import annotations

import contextlib
import json
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.db.base import session_scope
from app.db.repositories import CallRepository
from app.enums import CallOutcome
from app.logging_config import get_logger
from app.pipeline.engine import LeadContext
from app.pipeline.persistence import DbTurnSink
from app.pipeline.pipecat_runner import build_session, pipecat_available, run_twilio_pipeline
from app.services.call_service import CallService
from app.telephony.twilio_client import build_hangup_twiml, build_stream_twiml, get_telephony

router = APIRouter(prefix="/twilio", tags=["twilio"])
log = get_logger(__name__)

TWIML_MEDIA_TYPE = "application/xml"


async def _verified_form(request: Request) -> dict[str, str]:
    """Parse the form body after validating the Twilio signature."""
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    telephony = get_telephony()
    url = str(request.url)
    signature = request.headers.get("X-Twilio-Signature")
    if not telephony.validate_signature(url, params, signature):
        log.warning("twilio.invalid_signature", url=url)
        raise HTTPException(status_code=403, detail="invalid Twilio signature")
    return params


def _call_id_from(request: Request, params: dict[str, str]) -> uuid.UUID | None:
    raw = request.query_params.get("call_id") or params.get("call_id")
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


@router.post("/voice", summary="Answer webhook: connect the call to the media stream")
async def voice_webhook(request: Request) -> Response:
    params = await _verified_form(request)
    call_id = _call_id_from(request, params)
    call_sid = params.get("CallSid")
    if call_id is None:
        log.error("twilio.voice_missing_call_id", call_sid=call_sid)
        return Response(
            content=build_hangup_twiml("Sorry, there was a configuration problem."),
            media_type=TWIML_MEDIA_TYPE,
        )

    if call_sid:
        async with session_scope() as session:
            repo = CallRepository(session)
            if await repo.get(call_id) is not None:
                await repo.attach_sid(call_id, call_sid)

    twiml = build_stream_twiml(
        websocket_url=get_telephony().stream_url(str(call_id)), call_id=str(call_id)
    )
    log.info("twilio.voice_webhook", call_id=str(call_id), call_sid=call_sid)
    return Response(content=twiml, media_type=TWIML_MEDIA_TYPE)


@router.post("/amd", summary="Async answering-machine-detection callback")
async def amd_webhook(request: Request) -> dict[str, object]:
    params = await _verified_form(request)
    call_id = _call_id_from(request, params)
    answered_by = params.get("AnsweredBy")
    if call_id is None:
        return {"ok": False, "reason": "missing call_id"}
    async with session_scope() as session:
        result = await CallService(session).handle_amd(call_id, answered_by)
    log.info("twilio.amd", **{k: v for k, v in result.items() if k != "reason"})
    return {"ok": True, **result}


@router.post("/status", summary="Call lifecycle status callback")
async def status_webhook(request: Request) -> dict[str, object]:
    params = await _verified_form(request)
    call_id = _call_id_from(request, params)
    status_value = params.get("CallStatus", "")
    duration = params.get("CallDuration")
    if call_id is None:
        return {"ok": False, "reason": "missing call_id"}
    async with session_scope() as session:
        result = await CallService(session).handle_status(
            call_id, status_value, float(duration) if duration else None
        )
    log.info("twilio.status", call_id=str(call_id), status=status_value)
    return {"ok": True, **result}


@router.websocket("/stream")
async def media_stream(websocket: WebSocket) -> None:
    """Twilio Media Streams socket: run one call through the Pipecat pipeline."""
    await websocket.accept()
    settings = get_settings()
    call_id: uuid.UUID | None = None
    stream_sid: str | None = None
    call_sid: str | None = None

    # Twilio sends `connected` then `start`; the custom parameter carries our id.
    try:
        for _ in range(3):
            message = json.loads(await websocket.receive_text())
            event = message.get("event")
            if event == "start":
                start = message.get("start", {})
                stream_sid = start.get("streamSid") or message.get("streamSid")
                call_sid = start.get("callSid")
                raw_id = (start.get("customParameters") or {}).get("call_id")
                if raw_id:
                    try:
                        call_id = uuid.UUID(raw_id)
                    except ValueError:
                        call_id = None
                break
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        log.error("twilio.stream_handshake_failed", error=str(exc)[:200])
        await websocket.close(code=1011)
        return

    if call_id is None:
        raw_id = websocket.query_params.get("call_id")
        if raw_id:
            try:
                call_id = uuid.UUID(raw_id)
            except ValueError:
                call_id = None
    if call_id is None:
        log.error("twilio.stream_missing_call_id", call_sid=call_sid)
        await websocket.close(code=1008)
        return

    async with session_scope() as session:
        call = await CallRepository(session).get(call_id)
        if call is None:
            log.error("twilio.stream_unknown_call", call_id=str(call_id))
            await websocket.close(code=1008)
            return
        lead_context = LeadContext.from_model(call.lead)

    if not pipecat_available():
        log.error("twilio.stream_pipecat_missing", call_id=str(call_id))
        async with session_scope() as session:
            await CallRepository(session).finalize(
                call_id,
                outcome=CallOutcome.FAILED,
                reason='pipecat is not installed (pip install -e ".[voice]")',
            )
        await websocket.close(code=1011)
        return

    session_obj = build_session(call_id, lead_context, sink=DbTurnSink(call_id))
    log.info(
        "twilio.stream_started",
        call_id=str(call_id),
        stream_sid=stream_sid,
        business=lead_context.business_name,
    )
    try:
        await run_twilio_pipeline(websocket, session_obj, stream_sid=stream_sid, call_sid=call_sid)
    except Exception as exc:  # noqa: BLE001
        log.error("twilio.stream_failed", call_id=str(call_id), error=str(exc)[:300])
        if not session_obj.engine.ended:
            await session_obj.engine.abort(CallOutcome.FAILED, f"stream error: {exc}")
    finally:
        async with session_scope() as db_session:
            await CallService(db_session).finalize_from_engine(call_id, session_obj.engine)
        if settings.app_env != "test":
            with contextlib.suppress(Exception):  # socket may already be gone
                await websocket.close()
