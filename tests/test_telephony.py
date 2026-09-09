"""Telephony: AMD classification, retry backoff, TwiML and the compliance gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import get_settings
from app.enums import AnsweredBy, CallOutcome
from app.telephony.amd import backoff_delay, classify_answered_by, next_attempt_at
from app.telephony.compliance import check_compliance, in_calling_window
from app.telephony.twilio_client import build_hangup_twiml, build_stream_twiml


@pytest.mark.parametrize(
    "raw,machine,hangup,outcome",
    [
        ("human", False, False, None),
        ("unknown", False, False, None),
        ("machine_start", True, True, CallOutcome.VOICEMAIL),
        ("machine_end_beep", True, True, CallOutcome.VOICEMAIL),
        ("machine_end_silence", True, True, CallOutcome.VOICEMAIL),
        ("fax", True, True, CallOutcome.FAILED),
        ("nonsense", False, False, None),
    ],
)
def test_amd_classification(raw, machine, hangup, outcome):
    decision = classify_answered_by(raw)
    assert decision.is_machine is machine
    assert decision.should_hang_up is hangup
    assert decision.outcome is outcome


def test_amd_unknown_is_treated_as_human():
    assert classify_answered_by(None).answered_by is AnsweredBy.UNKNOWN
    assert not classify_answered_by(None).should_hang_up


def test_backoff_schedule_is_bounded():
    schedule = [60, 240, 1440]
    assert backoff_delay(1, schedule) == timedelta(minutes=60)
    assert backoff_delay(2, schedule) == timedelta(minutes=240)
    # CALL_MAX_ATTEMPTS defaults to 3, so there is no fourth attempt.
    assert backoff_delay(3, schedule) is None
    assert backoff_delay(9, schedule) is None


def test_next_attempt_at_is_in_the_future():
    now = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    assert next_attempt_at(1, now=now) == now + timedelta(minutes=60)
    assert next_attempt_at(5, now=now) is None


def test_stream_twiml_contains_the_socket_and_call_id():
    twiml = build_stream_twiml(
        websocket_url="wss://example.ngrok.app/twilio/stream?call_id=abc", call_id="abc"
    )
    assert "<Connect>" in twiml and "<Stream" in twiml
    assert "call_id" in twiml and "abc" in twiml
    assert "&amp;" in twiml or "?" in twiml  # the URL is XML-escaped


def test_hangup_twiml():
    assert "<Hangup />" in build_hangup_twiml("bye")


def test_compliance_blocks_dnc_numbers():
    decision = check_compliance(do_not_call=True, phone="+911140001001")
    assert not decision.allowed
    assert "do_not_call" in decision.reason


def test_compliance_requires_e164():
    decision = check_compliance(do_not_call=False, phone="011-4000-1001")
    assert not decision.allowed
    assert "E.164" in decision.reason


def test_compliance_allows_a_clean_lead():
    assert check_compliance(do_not_call=False, phone="+911140001001").allowed


def test_calling_window_is_enforced(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "calling_window_start_hour", 9)
    monkeypatch.setattr(settings, "calling_window_end_hour", 19)
    midnight = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)  # 07:30 IST
    assert not in_calling_window(midnight, settings)
    afternoon = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)  # 14:30 IST
    assert in_calling_window(afternoon, settings)
    decision = check_compliance(
        do_not_call=False, phone="+911140001001", now=midnight, settings=settings
    )
    assert not decision.allowed


def test_production_requires_dlt_registration(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "compliance_dlt_registered", False)
    decision = check_compliance(do_not_call=False, phone="+911140001001", settings=settings)
    assert not decision.allowed
    assert "DLT" in decision.reason
