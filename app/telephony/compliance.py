"""Compliance gate for Indian outbound calling (Project-Details.md §10).

Dialling is blocked unless every check passes:
  * the lead is not flagged `do_not_call` (National DNC / customer request)
  * the local time is inside the permitted calling window
  * in production, DLT registration has been completed

This is deliberately enforced in code rather than in a runbook — it is the one
class of mistake that costs money and goodwill.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings, get_settings


@dataclass(frozen=True)
class ComplianceDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def in_calling_window(now: datetime | None = None, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    try:
        tz = ZoneInfo(settings.calling_timezone)
    except Exception:  # pragma: no cover - tzdata missing
        tz = ZoneInfo("UTC")
    local = (now or datetime.now(tz)).astimezone(tz)
    return settings.calling_window_start_hour <= local.hour < settings.calling_window_end_hour


def check_compliance(
    *,
    do_not_call: bool,
    phone: str | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> ComplianceDecision:
    """Evaluate every gate and return a single allow/deny decision."""
    settings = settings or get_settings()
    reasons: list[str] = []

    if settings.compliance_dnc_check_enabled and do_not_call:
        reasons.append("lead is flagged do_not_call (DNC registry or explicit request)")
    if not phone or not phone.strip().startswith("+"):
        reasons.append("phone number must be in E.164 format (+CountryCode...)")
    if not in_calling_window(now, settings):
        reasons.append(
            f"outside the permitted calling window "
            f"{settings.calling_window_start_hour:02d}:00-{settings.calling_window_end_hour:02d}:00 "
            f"{settings.calling_timezone}"
        )
    if settings.app_env == "production" and not settings.compliance_dlt_registered:
        reasons.append("TRAI DLT registration is not marked complete (COMPLIANCE_DLT_REGISTERED)")

    return ComplianceDecision(allowed=not reasons, reasons=tuple(reasons))


__all__ = ["ComplianceDecision", "check_compliance", "in_calling_window"]
