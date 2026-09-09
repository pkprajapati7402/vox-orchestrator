"""Speech-to-text: Groq Whisper (primary) with a Deepgram fallback.

Both clients return the same `Transcript` shape, including a normalised
confidence score, because the ASR-confidence guard in the engine depends on it.
"""

from __future__ import annotations

import abc
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)


class STTError(RuntimeError):
    def __init__(self, message: str, *, provider: str = "", retryable: bool = True) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float
    duration_seconds: float
    provider: str
    model: str
    language: str | None = None
    latency_ms: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class STTClient(abc.ABC):
    provider = "unknown"
    model = ""

    @abc.abstractmethod
    async def transcribe(self, audio: bytes, *, mime_type: str = "audio/wav") -> Transcript: ...

    def is_configured(self) -> bool:  # pragma: no cover - overridden
        return True

    async def aclose(self) -> None:  # pragma: no cover
        return None


def _confidence_from_logprob(avg_logprob: float | None, no_speech_prob: float | None) -> float:
    """Map Whisper's `avg_logprob`/`no_speech_prob` onto a 0-1 confidence."""
    if avg_logprob is None:
        return 0.8 if not no_speech_prob else max(0.0, 1.0 - no_speech_prob)
    confidence = math.exp(max(min(avg_logprob, 0.0), -5.0))
    if no_speech_prob is not None:
        confidence *= max(0.0, 1.0 - no_speech_prob)
    return round(min(max(confidence, 0.0), 1.0), 4)


class GroqWhisperSTT(STTClient):
    """Groq-hosted Whisper (`whisper-large-v3-turbo`)."""

    provider = "groq"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.groq_api_key
        self.model = model or settings.groq_stt_model
        self.base_url = (base_url or settings.groq_base_url).rstrip("/")
        self.language = settings.stt_language
        self._client = client

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def transcribe(self, audio: bytes, *, mime_type: str = "audio/wav") -> Transcript:
        if not self.is_configured():
            raise STTError("GROQ_API_KEY is not set", provider=self.provider, retryable=False)
        started = time.perf_counter()
        files = {"file": ("audio.wav", audio, mime_type)}
        data: dict[str, Any] = {
            "model": self.model,
            "response_format": "verbose_json",
            "temperature": "0",
        }
        if self.language:
            data["language"] = self.language
        try:
            response = await self._http().post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                files=files,
                data=data,
            )
        except httpx.HTTPError as exc:
            raise STTError(f"groq stt transport error: {exc}", provider=self.provider) from exc
        if response.status_code >= 400:
            raise STTError(
                f"groq stt http {response.status_code}: {response.text[:200]}",
                provider=self.provider,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        body = response.json()
        segments = body.get("segments") or []
        avg_logprob = (
            sum(seg.get("avg_logprob", -0.5) for seg in segments) / len(segments)
            if segments
            else None
        )
        no_speech = (
            sum(seg.get("no_speech_prob", 0.0) for seg in segments) / len(segments)
            if segments
            else None
        )
        return Transcript(
            text=(body.get("text") or "").strip(),
            confidence=_confidence_from_logprob(avg_logprob, no_speech),
            duration_seconds=float(body.get("duration") or 0.0),
            provider=self.provider,
            model=self.model,
            language=body.get("language"),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


class DeepgramSTT(STTClient):
    """Deepgram Nova-2 (phonecall model) — fallback ASR."""

    provider = "deepgram"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.deepgram_api_key
        self.model = model or settings.deepgram_stt_model
        self.language = settings.stt_language
        self._client = client

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def transcribe(self, audio: bytes, *, mime_type: str = "audio/wav") -> Transcript:
        if not self.is_configured():
            raise STTError("DEEPGRAM_API_KEY is not set", provider=self.provider, retryable=False)
        started = time.perf_counter()
        params = {"model": self.model, "smart_format": "true", "language": self.language}
        try:
            response = await self._http().post(
                "https://api.deepgram.com/v1/listen",
                headers={"Authorization": f"Token {self.api_key}", "Content-Type": mime_type},
                params=params,
                content=audio,
            )
        except httpx.HTTPError as exc:
            raise STTError(f"deepgram transport error: {exc}", provider=self.provider) from exc
        if response.status_code >= 400:
            raise STTError(
                f"deepgram http {response.status_code}: {response.text[:200]}",
                provider=self.provider,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        body = response.json()
        channel = ((body.get("results") or {}).get("channels") or [{}])[0]
        alternative = (channel.get("alternatives") or [{}])[0]
        duration = float(((body.get("metadata") or {}).get("duration")) or 0.0)
        return Transcript(
            text=(alternative.get("transcript") or "").strip(),
            confidence=float(alternative.get("confidence") or 0.0),
            duration_seconds=duration,
            provider=self.provider,
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


class STTRouter(STTClient):
    """Primary STT with automatic failover to the secondary provider."""

    provider = "router"

    def __init__(self, primary: STTClient | None = None, fallback: STTClient | None = None) -> None:
        settings = get_settings()
        if primary is None:
            primary = GroqWhisperSTT() if settings.stt_primary_provider == "groq" else DeepgramSTT()
        if fallback is None:
            fallback = (
                DeepgramSTT() if settings.stt_primary_provider == "groq" else GroqWhisperSTT()
            )
        self.primary = primary
        self.fallback = fallback

    @property
    def model(self) -> str:  # type: ignore[override]
        return self.primary.model

    def is_configured(self) -> bool:
        return self.primary.is_configured() or self.fallback.is_configured()

    async def transcribe(self, audio: bytes, *, mime_type: str = "audio/wav") -> Transcript:
        last: Exception | None = None
        for client in (self.primary, self.fallback):
            if not client.is_configured():
                continue
            try:
                return await client.transcribe(audio, mime_type=mime_type)
            except STTError as exc:
                last = exc
                log.warning("stt.provider_failed", provider=client.provider, error=str(exc)[:200])
        raise STTError(f"all STT providers failed: {last}", provider="router", retryable=False)

    async def aclose(self) -> None:
        await self.primary.aclose()
        await self.fallback.aclose()


__all__ = ["DeepgramSTT", "GroqWhisperSTT", "STTClient", "STTError", "STTRouter", "Transcript"]
