"""Text-to-speech: ElevenLabs (primary) with a self-hosted Piper fallback.

The fallback matters commercially: ElevenLabs' free character budget runs out
long before a calling campaign does, and Piper keeps the agent talking for free.
"""

from __future__ import annotations

import abc
import asyncio
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import get_settings
from app.logging_config import get_logger

log = get_logger(__name__)


class TTSError(RuntimeError):
    def __init__(self, message: str, *, provider: str = "", retryable: bool = True) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


@dataclass(frozen=True)
class SpeechAudio:
    audio: bytes
    mime_type: str
    characters: int
    provider: str
    model: str
    latency_ms: int = 0


class TTSClient(abc.ABC):
    provider = "unknown"
    model = ""

    @abc.abstractmethod
    async def synthesize(self, text: str) -> SpeechAudio: ...

    def is_configured(self) -> bool:  # pragma: no cover - overridden
        return True

    async def aclose(self) -> None:  # pragma: no cover
        return None


class ElevenLabsTTS(TTSClient):
    provider = "elevenlabs"

    def __init__(
        self,
        api_key: str | None = None,
        voice_id: str | None = None,
        model: str | None = None,
        output_format: str = "ulaw_8000",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.elevenlabs_api_key
        self.voice_id = voice_id or settings.elevenlabs_voice_id
        self.model = model or settings.elevenlabs_model
        # ulaw_8000 is what Twilio Media Streams expects.
        self.output_format = output_format
        self._client = client

    def is_configured(self) -> bool:
        return bool(self.api_key and self.voice_id)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def synthesize(self, text: str) -> SpeechAudio:
        if not self.is_configured():
            raise TTSError(
                "ELEVENLABS_API_KEY/voice not set", provider=self.provider, retryable=False
            )
        started = time.perf_counter()
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
        try:
            response = await self._http().post(
                url,
                headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
                params={"output_format": self.output_format},
                json={
                    "text": text,
                    "model_id": self.model,
                    "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
                },
            )
        except httpx.HTTPError as exc:
            raise TTSError(f"elevenlabs transport error: {exc}", provider=self.provider) from exc
        if response.status_code >= 400:
            raise TTSError(
                f"elevenlabs http {response.status_code}: {response.text[:200]}",
                provider=self.provider,
                retryable=response.status_code in {429} or response.status_code >= 500,
            )
        return SpeechAudio(
            audio=response.content,
            mime_type="audio/basic" if self.output_format.startswith("ulaw") else "audio/mpeg",
            characters=len(text),
            provider=self.provider,
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


class PiperTTS(TTSClient):
    """Self-hosted Piper — free, offline, used when ElevenLabs is exhausted."""

    provider = "piper"

    def __init__(self, binary: str | None = None, model_path: str | None = None) -> None:
        settings = get_settings()
        self.binary = binary or settings.piper_binary
        self.model_path = model_path or settings.piper_model_path
        self.model = "piper"

    def is_configured(self) -> bool:
        return bool(shutil.which(self.binary)) and Path(self.model_path).exists()

    async def synthesize(self, text: str) -> SpeechAudio:
        if not self.is_configured():
            raise TTSError(
                f"piper binary '{self.binary}' or model '{self.model_path}' not available",
                provider=self.provider,
                retryable=False,
            )
        started = time.perf_counter()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            out_path = Path(handle.name)
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                "--model",
                self.model_path,
                "--output_file",
                str(out_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate(text.encode("utf-8"))
            if process.returncode != 0:
                raise TTSError(
                    f"piper exited {process.returncode}: {stderr.decode()[:200]}",
                    provider=self.provider,
                    retryable=False,
                )
            audio = out_path.read_bytes()
        finally:
            out_path.unlink(missing_ok=True)
        return SpeechAudio(
            audio=audio,
            mime_type="audio/wav",
            characters=len(text),
            provider=self.provider,
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


class TTSRouter(TTSClient):
    """ElevenLabs first, Piper when it errors, quota-limits, or is unset."""

    provider = "router"

    def __init__(self, primary: TTSClient | None = None, fallback: TTSClient | None = None) -> None:
        settings = get_settings()
        if primary is None:
            primary = (
                ElevenLabsTTS() if settings.tts_primary_provider == "elevenlabs" else PiperTTS()
            )
        if fallback is None:
            fallback = (
                PiperTTS() if settings.tts_primary_provider == "elevenlabs" else ElevenLabsTTS()
            )
        self.primary = primary
        self.fallback = fallback

    @property
    def model(self) -> str:  # type: ignore[override]
        return self.primary.model

    def is_configured(self) -> bool:
        return self.primary.is_configured() or self.fallback.is_configured()

    async def synthesize(self, text: str) -> SpeechAudio:
        last: Exception | None = None
        for client in (self.primary, self.fallback):
            if not client.is_configured():
                continue
            try:
                return await client.synthesize(text)
            except TTSError as exc:
                last = exc
                log.warning("tts.provider_failed", provider=client.provider, error=str(exc)[:200])
        raise TTSError(f"all TTS providers failed: {last}", provider="router", retryable=False)

    async def aclose(self) -> None:
        await self.primary.aclose()
        await self.fallback.aclose()


__all__ = ["ElevenLabsTTS", "PiperTTS", "SpeechAudio", "TTSClient", "TTSError", "TTSRouter"]
