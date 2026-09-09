"""Cost tracking and the rate card (Project-Details.md §8)."""

from __future__ import annotations

from app.costs.rates import llm_rate, stt_rate_per_second, tts_rate_per_char
from app.costs.tracker import CostAccumulator


def test_llm_cost_uses_the_model_rate_card():
    rate = llm_rate("llama-3.3-70b-versatile")
    assert rate.cost(1_000_000, 0) == 0.59
    assert round(rate.cost(0, 1_000_000), 6) == 0.79
    # Unknown models fall back to the default rate rather than costing zero.
    assert llm_rate("some-new-model").input_per_mtok > 0


def test_accumulator_sums_every_component():
    cost = CostAccumulator()
    cost.add_stt(120, "whisper-large-v3-turbo")
    cost.add_llm(10_000, 2_000, "llama-3.3-70b-versatile")
    cost.add_tts(1_000, "eleven_turbo_v2_5")
    cost.add_telephony(90)

    snapshot = cost.snapshot()
    assert snapshot.stt_seconds == 120
    assert snapshot.llm_input_tokens == 10_000
    assert snapshot.tts_characters == 1_000
    assert snapshot.telephony_minutes == 1.5

    expected_stt = 120 * stt_rate_per_second("whisper-large-v3-turbo")
    expected_llm = llm_rate("llama-3.3-70b-versatile").cost(10_000, 2_000)
    expected_tts = 1_000 * tts_rate_per_char("eleven_turbo_v2_5")
    assert round(snapshot.stt_cost_usd, 8) == round(expected_stt, 8)
    assert round(snapshot.llm_cost_usd, 8) == round(expected_llm, 8)
    assert round(snapshot.tts_cost_usd, 8) == round(expected_tts, 8)
    assert snapshot.cost_usd > 0
    assert round(snapshot.cost_usd, 6) == round(
        snapshot.stt_cost_usd
        + snapshot.llm_cost_usd
        + snapshot.tts_cost_usd
        + snapshot.telephony_cost_usd,
        6,
    )


def test_telephony_is_billed_in_whole_minutes():
    cost = CostAccumulator()
    cost.add_telephony(61)  # 1:01 is billed as two minutes
    assert cost.per_model["twilio_outbound_in"]["cost"] > 0
    assert round(cost.telephony_cost_usd, 6) == round(2 * 0.108, 6)


def test_piper_fallback_is_free():
    cost = CostAccumulator()
    cost.add_tts(5_000, "piper")
    assert cost.tts_cost_usd == 0.0


def test_cost_log_payload_matches_the_table_columns():
    cost = CostAccumulator()
    cost.add_llm(100, 50, "gemini-2.0-flash")
    payload = cost.to_cost_log_payload()
    expected_columns = {
        "stt_seconds",
        "llm_input_tokens",
        "llm_output_tokens",
        "tts_characters",
        "telephony_minutes",
        "stt_cost_usd",
        "llm_cost_usd",
        "tts_cost_usd",
        "telephony_cost_usd",
        "cost_usd",
        "breakdown",
    }
    assert set(payload) == expected_columns


def test_negative_usage_is_clamped():
    cost = CostAccumulator()
    cost.add_stt(-10)
    cost.add_llm(-5, -5)
    cost.add_tts(-100)
    assert cost.total_usd == 0.0
