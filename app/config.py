"""Application configuration.

Every setting is sourced from the environment (see `.env.example`) and has a
development-safe default, so the API, the CLI, the eval harness and the test
suite all boot without any credentials configured.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["groq", "gemini", "mock", "none"]
STTProvider = Literal["groq", "deepgram"]
TTSProvider = Literal["elevenlabs", "piper"]


class Settings(BaseSettings):
    """Typed view over the process environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- core -----------------------------------------------------------
    app_env: Literal["development", "staging", "production", "test"] = "development"
    log_level: str = "INFO"
    log_json: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    public_base_url: str = "http://localhost:8000"

    # --- telephony ------------------------------------------------------
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    twilio_validate_signatures: bool = True
    twilio_amd_enabled: bool = True
    twilio_amd_timeout_seconds: int = 15
    twilio_call_timeout_seconds: int = 30
    twilio_max_call_seconds: int = 420

    # --- llm ------------------------------------------------------------
    groq_api_key: str = ""
    groq_llm_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    gemini_api_key: str = ""
    gemini_llm_model: str = "gemini-2.0-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    llm_primary_provider: LLMProvider = "groq"
    llm_fallback_provider: LLMProvider = "gemini"
    llm_timeout_seconds: float = 8.0
    llm_max_retries: int = 1
    llm_temperature: float = 0.3
    llm_max_tokens: int = 400

    # --- stt ------------------------------------------------------------
    groq_stt_model: str = "whisper-large-v3-turbo"
    deepgram_api_key: str = ""
    deepgram_stt_model: str = "nova-2-phonecall"
    stt_primary_provider: STTProvider = "groq"
    stt_language: str = "en"
    stt_min_confidence: float = 0.55

    # --- tts ------------------------------------------------------------
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    elevenlabs_model: str = "eleven_turbo_v2_5"
    tts_primary_provider: TTSProvider = "elevenlabs"
    piper_binary: str = "piper"
    piper_model_path: str = "./models/en_US-lessac-medium.onnx"

    # --- data -----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./vox_orchestrator.sqlite3"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 5
    redis_url: str = ""
    session_ttl_seconds: int = 3600

    # --- conversation policy -------------------------------------------
    agent_name: str = "Riya"
    agency_name: str = "Northstar Digital"
    agent_callback_number: str = "+911140000000"
    max_objection_cycles: int = 3
    max_conversation_turns: int = 40
    silence_timeout_seconds: float = 7.0
    max_asr_repeat_requests: int = 1
    max_tool_validation_retries: int = 1

    # --- retry / backoff -------------------------------------------------
    call_retry_backoff_minutes: str = "60,240,1440"
    call_max_attempts: int = 3

    # --- research --------------------------------------------------------
    research_enabled: bool = True
    research_timeout_seconds: float = 8.0
    research_cache_days: int = 30
    research_user_agent: str = "vox-orchestrator/1.0"

    # --- observability ---------------------------------------------------
    tracing_enabled: bool = False
    phoenix_collector_endpoint: str = "http://localhost:6006/v1/traces"
    otel_service_name: str = "vox-orchestrator"

    # --- compliance ------------------------------------------------------
    compliance_dnc_check_enabled: bool = True
    compliance_dlt_registered: bool = False
    calling_window_start_hour: int = 9
    calling_window_end_hour: int = 19
    calling_timezone: str = "Asia/Kolkata"

    # --- eval ------------------------------------------------------------
    eval_llm_provider: LLMProvider = "mock"
    eval_max_turns: int = 25
    eval_report_dir: str = "eval/reports"

    # --- validators ------------------------------------------------------
    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    # --- derived helpers --------------------------------------------------
    @property
    def retry_backoff_minutes(self) -> list[int]:
        """Backoff schedule for unanswered calls, in minutes."""
        out: list[int] = []
        for chunk in self.call_retry_backoff_minutes.split(","):
            chunk = chunk.strip()
            if chunk.isdigit():
                out.append(int(chunk))
        return out or [60]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def websocket_base_url(self) -> str:
        """`wss://` form of the public URL, used in the Twilio <Stream> verb."""
        base = self.public_base_url
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :]
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :]
        return base

    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_phone_number)

    def llm_configured(self) -> bool:
        return bool(self.groq_api_key or self.gemini_api_key)


DEFAULT_SETTINGS_FIELDS = Field  # re-export guard for linters


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reload_settings() -> Settings:
    """Clear the cache and re-read the environment (used in tests)."""
    get_settings.cache_clear()
    return get_settings()


settings = get_settings()
