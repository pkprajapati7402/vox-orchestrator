"""Provider rate card.

Published list prices, USD, as configured for this project. They are data, not
logic — update them here when a provider changes pricing and every historical
report can be re-priced by replaying `CostTracker` against the stored usage.

Sources (checked at time of writing, all subject to change):
  * Groq        — per-million-token pricing for Llama 3.3 70B; Whisper billed per audio hour
  * Google      — Gemini 2.0 Flash per-million-token pricing (paid tier; free tier is $0)
  * Deepgram    — Nova-2 phonecall, per minute
  * ElevenLabs  — Turbo v2.5, effective per-1k-character cost on the Creator tier
  * Twilio      — outbound voice, per minute (India destination, programmable voice)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMRate:
    """USD per 1,000,000 tokens."""

    input_per_mtok: float
    output_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok
        ) / 1_000_000


LLM_RATES: dict[str, LLMRate] = {
    "llama-3.3-70b-versatile": LLMRate(0.59, 0.79),
    "llama-3.1-8b-instant": LLMRate(0.05, 0.08),
    "gemini-2.0-flash": LLMRate(0.10, 0.40),
    "gemini-1.5-flash": LLMRate(0.075, 0.30),
    "mock-policy-v2": LLMRate(0.0, 0.0),
}
DEFAULT_LLM_RATE = LLMRate(0.59, 0.79)

#: USD per second of audio transcribed.
STT_RATES_PER_SECOND: dict[str, float] = {
    "whisper-large-v3-turbo": 0.04 / 3600,  # $0.04 / audio hour
    "whisper-large-v3": 0.111 / 3600,  # $0.111 / audio hour
    "nova-2-phonecall": 0.0059 / 60,  # $0.0059 / minute
}
DEFAULT_STT_RATE_PER_SECOND = 0.04 / 3600

#: USD per character synthesised.
TTS_RATES_PER_CHAR: dict[str, float] = {
    "eleven_turbo_v2_5": 0.09 / 1000,  # ~$0.09 / 1k characters
    "eleven_multilingual_v2": 0.18 / 1000,
    "piper": 0.0,  # self-hosted
}
DEFAULT_TTS_RATE_PER_CHAR = 0.09 / 1000

#: USD per minute of outbound telephony.
TELEPHONY_RATE_PER_MINUTE: dict[str, float] = {
    "twilio_outbound_in": 0.108,
    "twilio_outbound_us": 0.014,
}
DEFAULT_TELEPHONY_RATE_PER_MINUTE = 0.108


def llm_rate(model: str | None) -> LLMRate:
    return LLM_RATES.get((model or "").strip(), DEFAULT_LLM_RATE)


def stt_rate_per_second(model: str | None) -> float:
    return STT_RATES_PER_SECOND.get((model or "").strip(), DEFAULT_STT_RATE_PER_SECOND)


def tts_rate_per_char(model: str | None) -> float:
    return TTS_RATES_PER_CHAR.get((model or "").strip(), DEFAULT_TTS_RATE_PER_CHAR)


def telephony_rate_per_minute(route: str = "twilio_outbound_in") -> float:
    return TELEPHONY_RATE_PER_MINUTE.get(route, DEFAULT_TELEPHONY_RATE_PER_MINUTE)


__all__ = [
    "DEFAULT_LLM_RATE",
    "DEFAULT_STT_RATE_PER_SECOND",
    "DEFAULT_TELEPHONY_RATE_PER_MINUTE",
    "DEFAULT_TTS_RATE_PER_CHAR",
    "LLMRate",
    "LLM_RATES",
    "STT_RATES_PER_SECOND",
    "TELEPHONY_RATE_PER_MINUTE",
    "TTS_RATES_PER_CHAR",
    "llm_rate",
    "stt_rate_per_second",
    "telephony_rate_per_minute",
    "tts_rate_per_char",
]
