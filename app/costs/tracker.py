"""Per-call cost accumulator (Project-Details.md §8).

Usage is recorded as it happens during the call — STT seconds, LLM tokens, TTS
characters, telephony minutes — and converted to dollars with the rate card.
`to_cost_log_payload()` maps 1:1 onto the `cost_logs` table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.costs.rates import (
    llm_rate,
    stt_rate_per_second,
    telephony_rate_per_minute,
    tts_rate_per_char,
)


@dataclass(frozen=True)
class CostSnapshot:
    stt_seconds: float
    llm_input_tokens: int
    llm_output_tokens: int
    tts_characters: int
    telephony_minutes: float
    stt_cost_usd: float
    llm_cost_usd: float
    tts_cost_usd: float
    telephony_cost_usd: float
    cost_usd: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "stt_seconds": round(self.stt_seconds, 3),
            "llm_input_tokens": self.llm_input_tokens,
            "llm_output_tokens": self.llm_output_tokens,
            "tts_characters": self.tts_characters,
            "telephony_minutes": round(self.telephony_minutes, 4),
            "stt_cost_usd": round(self.stt_cost_usd, 6),
            "llm_cost_usd": round(self.llm_cost_usd, 6),
            "tts_cost_usd": round(self.tts_cost_usd, 6),
            "telephony_cost_usd": round(self.telephony_cost_usd, 6),
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class CostAccumulator:
    """Mutable per-call usage counter. Cheap enough to update on every turn."""

    stt_seconds: float = 0.0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    tts_characters: int = 0
    telephony_seconds: float = 0.0

    stt_cost_usd: float = 0.0
    llm_cost_usd: float = 0.0
    tts_cost_usd: float = 0.0
    telephony_cost_usd: float = 0.0

    per_model: dict[str, dict[str, float]] = field(default_factory=dict)

    # --- recording -------------------------------------------------------
    def add_stt(self, seconds: float, model: str | None = None) -> float:
        seconds = max(float(seconds), 0.0)
        cost = seconds * stt_rate_per_second(model)
        self.stt_seconds += seconds
        self.stt_cost_usd += cost
        self._bump(model or "stt", seconds=seconds, cost=cost)
        return cost

    def add_llm(self, input_tokens: int, output_tokens: int, model: str | None = None) -> float:
        input_tokens = max(int(input_tokens), 0)
        output_tokens = max(int(output_tokens), 0)
        cost = llm_rate(model).cost(input_tokens, output_tokens)
        self.llm_input_tokens += input_tokens
        self.llm_output_tokens += output_tokens
        self.llm_cost_usd += cost
        self._bump(
            model or "llm", input_tokens=input_tokens, output_tokens=output_tokens, cost=cost
        )
        return cost

    def add_tts(self, characters: int, model: str | None = None) -> float:
        characters = max(int(characters), 0)
        cost = characters * tts_rate_per_char(model)
        self.tts_characters += characters
        self.tts_cost_usd += cost
        self._bump(model or "tts", characters=characters, cost=cost)
        return cost

    def add_telephony(self, seconds: float, route: str = "twilio_outbound_in") -> float:
        seconds = max(float(seconds), 0.0)
        # Twilio bills whole minutes; round up to keep the estimate honest.
        billed_minutes = -(-seconds // 60) if seconds else 0.0
        cost = billed_minutes * telephony_rate_per_minute(route)
        self.telephony_seconds += seconds
        self.telephony_cost_usd += cost
        self._bump(route, seconds=seconds, cost=cost)
        return cost

    def _bump(self, key: str, **values: float) -> None:
        bucket = self.per_model.setdefault(key, {})
        for name, value in values.items():
            bucket[name] = round(bucket.get(name, 0.0) + value, 6)

    # --- reading ---------------------------------------------------------
    @property
    def total_usd(self) -> float:
        return self.stt_cost_usd + self.llm_cost_usd + self.tts_cost_usd + self.telephony_cost_usd

    def snapshot(self) -> CostSnapshot:
        return CostSnapshot(
            stt_seconds=self.stt_seconds,
            llm_input_tokens=self.llm_input_tokens,
            llm_output_tokens=self.llm_output_tokens,
            tts_characters=self.tts_characters,
            telephony_minutes=self.telephony_seconds / 60.0,
            stt_cost_usd=self.stt_cost_usd,
            llm_cost_usd=self.llm_cost_usd,
            tts_cost_usd=self.tts_cost_usd,
            telephony_cost_usd=self.telephony_cost_usd,
            cost_usd=self.total_usd,
        )

    def to_cost_log_payload(self) -> dict[str, Any]:
        payload = self.snapshot().as_dict()
        payload["breakdown"] = {k: dict(v) for k, v in self.per_model.items()}
        return payload


__all__ = ["CostAccumulator", "CostSnapshot"]
