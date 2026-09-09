"""CLI smoke tests — the operator entrypoints must at least run and exit 0."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from app.cli import app

pytestmark = pytest.mark.usefixtures("db")
runner = CliRunner()


def test_doctor_reports_the_environment():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "vox-orchestrator environment" in result.stdout


def test_flow_show_prints_every_state():
    result = runner.invoke(app, ["flow", "show"])
    assert result.exit_code == 0
    assert "objection_handling" in result.stdout
    assert "schedule_meeting" in result.stdout


def test_db_init_then_import_and_list_leads(tmp_path):
    csv_path = tmp_path / "leads.csv"
    csv_path.write_text(
        "business_name,phone,category,address,website\n"
        "Glow Studio Salon,+911140001001,Salon,Hauz Khas,https://example.com\n"
        "Bean & Brew,+911140001002,Cafe,Green Park,\n"
    )
    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    imported = runner.invoke(app, ["leads", "import", str(csv_path)])
    assert imported.exit_code == 0
    assert "2 rows processed" in imported.stdout

    listed = runner.invoke(app, ["leads", "list"])
    assert listed.exit_code == 0
    assert "Glow Studio" in listed.stdout


def test_costs_report_runs_on_an_empty_database():
    result = runner.invoke(app, ["costs", "report", "--days", "7"])
    assert result.exit_code == 0
    assert "Call cost & outcome summary" in result.stdout


def test_simulate_records_a_full_conversation(tmp_path):
    import asyncio

    from app.db.base import session_scope
    from app.db.repositories import CallRepository, LeadRepository

    async def _seed() -> str:
        async with session_scope() as session:
            lead = await LeadRepository(session).create(
                business_name="Glow Studio Salon", phone="+911140001001", category="salon"
            )
            return str(lead.id)

    lead_id = asyncio.run(_seed())
    result = runner.invoke(app, ["call", "simulate", lead_id, "--persona", "interested_01"])
    assert result.exit_code == 0, result.output
    assert "AGENT" in result.stdout
    assert "meeting_booked" in result.stdout

    async def _transcript() -> int:
        async with session_scope() as session:
            calls = await CallRepository(session).list()
            turns = await CallRepository(session).transcript(calls[0].id)
            return len(turns)

    assert asyncio.run(_transcript()) > 4
