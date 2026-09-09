"""HTTP API: health, leads, calls, reports and the Twilio webhooks."""

from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import ASGITransport

from app.db.base import session_scope
from app.db.repositories import CallRepository, LeadRepository
from app.enums import CallOutcome

pytestmark = pytest.mark.usefixtures("db")


@pytest.fixture()
async def client():  # noqa: ANN201
    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


async def test_health_reports_dependencies(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert body["environment"] == "test"
    assert "pipecat_installed" in body
    assert "compliance" in body


async def test_root_points_at_the_docs(client):
    body = (await client.get("/")).json()
    assert body["service"] == "vox-orchestrator"


async def test_lead_crud_and_bulk_upsert(client):
    created = await client.post(
        "/leads",
        json={
            "business_name": "Glow Studio Salon",
            "phone": "+911140001001",
            "category": "salon",
            "address": "Hauz Khas",
        },
    )
    assert created.status_code == 201
    lead_id = created.json()["id"]

    listed = await client.get("/leads")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    fetched = await client.get(f"/leads/{lead_id}")
    assert fetched.json()["business_name"] == "Glow Studio Salon"

    bulk = await client.post(
        "/leads/bulk",
        json=[
            {"business_name": "Bean & Brew", "phone": "+911140001002"},
            {"business_name": "Glow Studio Salon", "phone": "+911140001001"},
        ],
    )
    assert bulk.json() == {"received": 2, "created": 1, "updated": 1}


async def test_unknown_lead_returns_404(client):
    response = await client.get(f"/leads/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_do_not_call_endpoint_blocks_dialling(client):
    lead_id = (
        await client.post("/leads", json={"business_name": "Blocked", "phone": "+911140001009"})
    ).json()["id"]
    flagged = await client.post(f"/leads/{lead_id}/do-not-call")
    assert flagged.json()["do_not_call"] is True

    dial = await client.post("/calls/dial", json={"lead_id": lead_id, "dry_run": True})
    assert dial.status_code == 409
    assert "do_not_call" in dial.json()["detail"]


async def test_dry_run_dial_creates_a_call_record(client):
    lead_id = (
        await client.post("/leads", json={"business_name": "Cafe", "phone": "+911140001003"})
    ).json()["id"]
    response = await client.post("/calls/dial", json={"lead_id": lead_id, "dry_run": True})
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True

    calls = await client.get("/calls")
    assert len(calls.json()) == 1
    detail = await client.get(f"/calls/{body['call_id']}")
    assert detail.status_code == 200
    assert detail.json()["turns"] == []


async def test_campaign_endpoint_is_dry_runnable(client):
    await client.post("/leads", json={"business_name": "Cafe", "phone": "+911140001004"})
    response = await client.post("/campaigns/run", json={"limit": 5, "dry_run": True})
    assert response.status_code == 200
    assert response.json()["dialed"] == 1


async def test_cost_report_endpoint(client):
    response = await client.get("/reports/costs?days=7")
    assert response.status_code == 200
    body = response.json()
    assert body["window_days"] == 7
    assert body["total_calls"] == 0


async def test_twilio_voice_webhook_returns_stream_twiml(client):
    lead_id = (
        await client.post("/leads", json={"business_name": "Cafe", "phone": "+911140001005"})
    ).json()["id"]
    call_id = (await client.post("/calls/dial", json={"lead_id": lead_id, "dry_run": True})).json()[
        "call_id"
    ]

    response = await client.post(
        f"/twilio/voice?call_id={call_id}",
        data={"CallSid": "CA123", "CallStatus": "in-progress"},
    )
    assert response.status_code == 200
    assert "<Stream" in response.text
    assert call_id in response.text

    async with session_scope() as session:
        call = await CallRepository(session).get(uuid.UUID(call_id))
        assert call.provider_call_sid == "CA123"


async def test_twilio_voice_webhook_without_call_id_hangs_up(client):
    response = await client.post("/twilio/voice", data={"CallSid": "CA999"})
    assert "<Hangup />" in response.text


async def test_twilio_amd_webhook_marks_voicemail(client):
    lead_id = (
        await client.post("/leads", json={"business_name": "Cafe", "phone": "+911140001006"})
    ).json()["id"]
    call_id = (await client.post("/calls/dial", json={"lead_id": lead_id, "dry_run": True})).json()[
        "call_id"
    ]

    response = await client.post(
        f"/twilio/amd?call_id={call_id}", data={"AnsweredBy": "machine_end_beep"}
    )
    assert response.json()["hung_up"] is True
    async with session_scope() as session:
        call = await CallRepository(session).get(uuid.UUID(call_id))
        assert call.outcome == CallOutcome.VOICEMAIL.value


async def test_twilio_status_webhook_records_no_answer(client):
    lead_id = (
        await client.post("/leads", json={"business_name": "Cafe", "phone": "+911140001007"})
    ).json()["id"]
    call_id = (await client.post("/calls/dial", json={"lead_id": lead_id, "dry_run": True})).json()[
        "call_id"
    ]

    response = await client.post(
        f"/twilio/status?call_id={call_id}", data={"CallStatus": "no-answer", "CallDuration": "0"}
    )
    assert response.json()["ok"] is True
    async with session_scope() as session:
        call = await CallRepository(session).get(uuid.UUID(call_id))
        lead = await LeadRepository(session).get(uuid.UUID(lead_id))
        assert call.outcome == CallOutcome.NO_ANSWER.value
        assert lead.next_attempt_at is not None


async def test_twilio_signature_is_enforced_when_enabled(client, monkeypatch):
    from app.config import get_settings
    from app.telephony.twilio_client import get_telephony

    settings = get_settings()
    monkeypatch.setattr(settings, "twilio_validate_signatures", True)
    monkeypatch.setattr(settings, "twilio_auth_token", "secret")
    monkeypatch.setattr(get_telephony(), "settings", settings)

    response = await client.post("/twilio/voice?call_id=abc", data={"CallSid": "CA1"})
    assert response.status_code == 403
