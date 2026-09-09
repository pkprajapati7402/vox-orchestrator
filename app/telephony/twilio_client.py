"""Twilio integration: outbound dialling with AMD, TwiML, signature validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from app.config import Settings, get_settings
from app.logging_config import get_logger

log = get_logger(__name__)


class TwilioError(RuntimeError):
    """Wraps any failure coming out of the Twilio REST client."""


@dataclass(frozen=True)
class PlacedCall:
    sid: str
    to: str
    from_: str
    status: str


def build_stream_twiml(
    *,
    websocket_url: str,
    call_id: str,
    greeting: str | None = None,
    max_call_seconds: int | None = None,
) -> str:
    """TwiML that hands the call's audio to our Pipecat websocket.

    `<Connect><Stream>` is bidirectional, which is what Pipecat's Twilio
    serializer expects; `<Parameter>` carries our internal call id so the
    websocket handler can look the call up without a database round trip on
    the `start` event.
    """
    settings = get_settings()
    limit = max_call_seconds or settings.twilio_max_call_seconds
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Response>"]
    if greeting:
        parts.append(f"  <Say>{_escape(greeting)}</Say>")
    parts.append("  <Connect>")
    parts.append(f'    <Stream url="{_escape(websocket_url)}">')
    parts.append(f'      <Parameter name="call_id" value="{_escape(call_id)}" />')
    parts.append("    </Stream>")
    parts.append("  </Connect>")
    parts.append(f'  <Pause length="{limit}" />')
    parts.append("</Response>")
    return "\n".join(parts)


def build_hangup_twiml(message: str | None = None) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Response>"]
    if message:
        parts.append(f"  <Say>{_escape(message)}</Say>")
    parts.append("  <Hangup />")
    parts.append("</Response>")
    return "\n".join(parts)


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


class TwilioTelephony:
    """Thin async-friendly wrapper over the (synchronous) Twilio REST client."""

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client

    # --- client ----------------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.settings.twilio_configured():
                raise TwilioError(
                    "Twilio is not configured — set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN "
                    "and TWILIO_PHONE_NUMBER"
                )
            try:
                from twilio.rest import Client
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise TwilioError("twilio package is not installed") from exc
            self._client = Client(self.settings.twilio_account_sid, self.settings.twilio_auth_token)
        return self._client

    def is_configured(self) -> bool:
        return self.settings.twilio_configured()

    # --- urls -------------------------------------------------------------
    def voice_webhook_url(self, call_id: str) -> str:
        query = urlencode({"call_id": call_id})
        return f"{self.settings.public_base_url}/twilio/voice?{query}"

    def status_callback_url(self, call_id: str) -> str:
        query = urlencode({"call_id": call_id})
        return f"{self.settings.public_base_url}/twilio/status?{query}"

    def amd_callback_url(self, call_id: str) -> str:
        query = urlencode({"call_id": call_id})
        return f"{self.settings.public_base_url}/twilio/amd?{query}"

    def stream_url(self, call_id: str) -> str:
        return f"{self.settings.websocket_base_url}/twilio/stream?call_id={call_id}"

    # --- actions -----------------------------------------------------------
    def place_call(self, to: str, call_id: str) -> PlacedCall:
        """Dial `to`, pointing Twilio at our voice webhook. Blocking (run in a thread)."""
        params: dict[str, Any] = {
            "to": to,
            "from_": self.settings.twilio_phone_number,
            "url": self.voice_webhook_url(call_id),
            "method": "POST",
            "status_callback": self.status_callback_url(call_id),
            "status_callback_method": "POST",
            "status_callback_event": ["initiated", "ringing", "answered", "completed"],
            "timeout": self.settings.twilio_call_timeout_seconds,
            "time_limit": self.settings.twilio_max_call_seconds,
        }
        if self.settings.twilio_amd_enabled:
            params.update(
                machine_detection="DetectMessageEnd",
                machine_detection_timeout=self.settings.twilio_amd_timeout_seconds,
                async_amd="true",
                async_amd_status_callback=self.amd_callback_url(call_id),
                async_amd_status_callback_method="POST",
            )
        try:
            created = self.client.calls.create(**params)
        except Exception as exc:  # noqa: BLE001 - twilio raises many types
            raise TwilioError(f"failed to place call to {to}: {exc}") from exc
        log.info("twilio.call_placed", sid=created.sid, to=to, call_id=call_id)
        return PlacedCall(
            sid=created.sid, to=to, from_=self.settings.twilio_phone_number, status=created.status
        )

    def hangup(self, call_sid: str) -> None:
        try:
            self.client.calls(call_sid).update(status="completed")
        except Exception as exc:  # noqa: BLE001
            raise TwilioError(f"failed to hang up {call_sid}: {exc}") from exc

    # --- security ----------------------------------------------------------
    def validate_signature(self, url: str, params: dict[str, Any], signature: str | None) -> bool:
        """Verify `X-Twilio-Signature`; skipped when validation is disabled."""
        if not self.settings.twilio_validate_signatures:
            return True
        if not signature or not self.settings.twilio_auth_token:
            return False
        try:
            from twilio.request_validator import RequestValidator
        except ImportError:  # pragma: no cover
            return False
        validator = RequestValidator(self.settings.twilio_auth_token)
        return bool(validator.validate(url, params, signature))


_telephony: TwilioTelephony | None = None


def get_telephony() -> TwilioTelephony:
    global _telephony
    if _telephony is None:
        _telephony = TwilioTelephony()
    return _telephony


__all__ = [
    "PlacedCall",
    "TwilioError",
    "TwilioTelephony",
    "build_hangup_twiml",
    "build_stream_twiml",
    "get_telephony",
]
