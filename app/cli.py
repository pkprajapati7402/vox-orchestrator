"""`vox` — operator CLI.

vox doctor                  # what is configured, what is missing
vox db init                 # create tables (SQLite dev / bootstrap)
vox leads import leads.csv  # load the Google Maps export
vox leads research          # fill research_notes
vox call dial <lead-id>     # place one real call
vox call simulate <lead-id> # text-only rehearsal, logged like a real call
vox campaign run            # dial everything that is due
vox costs report            # weekly cost / outcome summary
vox eval run                # offline persona suite
vox flow show               # print the state machine
"""

from __future__ import annotations

import asyncio
import csv
import json
import uuid
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from app.config import get_settings
from app.logging_config import configure_logging

app = typer.Typer(help="Vox-Orchestrator operator CLI", no_args_is_help=True)
db_app = typer.Typer(help="Database commands", no_args_is_help=True)
leads_app = typer.Typer(help="Lead commands", no_args_is_help=True)
call_app = typer.Typer(help="Call commands", no_args_is_help=True)
campaign_app = typer.Typer(help="Campaign commands", no_args_is_help=True)
costs_app = typer.Typer(help="Cost reporting", no_args_is_help=True)
eval_app = typer.Typer(help="Eval harness", no_args_is_help=True)
flow_app = typer.Typer(help="Conversation flow", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(leads_app, name="leads")
app.add_typer(call_app, name="call")
app.add_typer(campaign_app, name="campaign")
app.add_typer(costs_app, name="costs")
app.add_typer(eval_app, name="eval")
app.add_typer(flow_app, name="flow")

console = Console()


def _run(coro):  # noqa: ANN001, ANN202
    configure_logging()
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
@app.command()
def doctor() -> None:
    """Show which integrations are configured and which are missing."""
    from app.pipeline.pipecat_runner import pipecat_available
    from app.stt import STTRouter
    from app.telephony.compliance import in_calling_window
    from app.telephony.twilio_client import get_telephony
    from app.tts import TTSRouter

    settings = get_settings()
    table = Table(title="vox-orchestrator environment", show_lines=False)
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Detail")

    def row(name: str, ok: bool, detail: str) -> None:
        table.add_row(name, "[green]ok[/green]" if ok else "[yellow]missing[/yellow]", detail)

    row("environment", True, settings.app_env)
    row("database", True, "sqlite (dev)" if settings.is_sqlite else "postgres")
    row("redis", bool(settings.redis_url), settings.redis_url or "in-memory fallback")
    row("twilio", get_telephony().is_configured(), settings.twilio_phone_number or "-")
    row("llm groq", bool(settings.groq_api_key), settings.groq_llm_model)
    row("llm gemini", bool(settings.gemini_api_key), settings.gemini_llm_model)
    row("stt", STTRouter().is_configured(), settings.stt_primary_provider)
    row("tts", TTSRouter().is_configured(), settings.tts_primary_provider)
    row("pipecat (voice extra)", pipecat_available(), 'pip install -e ".\\[voice]"')
    row("public url", settings.public_base_url.startswith("https://"), settings.public_base_url)
    row("dlt registered", settings.compliance_dlt_registered, "required for production dialling")
    row(
        "calling window",
        in_calling_window(),
        f"{settings.calling_window_start_hour}-{settings.calling_window_end_hour} {settings.calling_timezone}",
    )
    console.print(table)


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------
@db_app.command("init")
def db_init() -> None:
    """Create every table defined by the ORM models."""
    from app.db.base import create_all

    _run(create_all())
    console.print("[green]tables created[/green]")


@db_app.command("reset")
def db_reset(yes: bool = typer.Option(False, "--yes", help="skip the confirmation prompt")) -> None:
    """Drop and recreate every table. Destructive."""
    from app.db.base import create_all, drop_all

    if not yes and not typer.confirm("This deletes all data. Continue?"):
        raise typer.Abort()

    async def _reset() -> None:
        await drop_all()
        await create_all()

    _run(_reset())
    console.print("[green]database reset[/green]")


# ---------------------------------------------------------------------------
# leads
# ---------------------------------------------------------------------------
@leads_app.command("import")
def leads_import(
    path: Path = typer.Argument(
        ..., exists=True, readable=True, help="CSV export of the lead list"
    ),
) -> None:
    """Import a Google Maps lead CSV (business_name, phone, category, address, website)."""
    from app.db.base import create_all, session_scope
    from app.db.repositories import LeadRepository

    async def _import() -> tuple[int, int]:
        settings = get_settings()
        if settings.is_sqlite:
            await create_all()
        created = 0
        total = 0
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        async with session_scope() as session:
            repo = LeadRepository(session)
            for row in rows:
                phone = (row.get("phone") or "").strip()
                name = (row.get("business_name") or "").strip()
                if not phone or not name:
                    continue
                total += 1
                _, was_created = await repo.upsert_by_phone(
                    phone,
                    business_name=name,
                    category=(row.get("category") or "").strip() or None,
                    address=(row.get("address") or "").strip() or None,
                    website=(row.get("website") or "").strip() or None,
                    contact_name=(row.get("contact_name") or "").strip() or None,
                )
                created += int(was_created)
        return total, created

    total, created = _run(_import())
    console.print(
        f"[green]{total} rows processed[/green] — {created} new, {total - created} updated"
    )


@leads_app.command("list")
def leads_list(limit: int = 20, status: str | None = None) -> None:
    """List leads."""
    from app.db.base import session_scope
    from app.db.repositories import LeadRepository

    async def _list():  # noqa: ANN202
        async with session_scope() as session:
            return await LeadRepository(session).list(status=status, limit=limit)

    leads = _run(_list())
    table = Table(title=f"leads ({len(leads)})")
    for column in ("id", "business", "phone", "status", "attempts", "researched"):
        table.add_column(column)
    for lead in leads:
        table.add_row(
            str(lead.id)[:8],
            lead.business_name,
            lead.phone,
            lead.status,
            str(lead.attempts),
            "yes" if lead.research_notes else "no",
        )
    console.print(table)


@leads_app.command("research")
def leads_research(limit: int = 25, force: bool = False) -> None:
    """Fill `research_notes` for leads that need it."""
    from app.db.base import session_scope
    from app.db.repositories import LeadRepository
    from app.llm.router import get_router
    from app.research.service import ResearchService

    async def _research() -> list[tuple[str, str]]:
        settings = get_settings()
        out: list[tuple[str, str]] = []
        service = ResearchService(llm=get_router())
        try:
            async with session_scope() as session:
                repo = LeadRepository(session)
                leads = await repo.needing_research(
                    limit=limit, cache_days=0 if force else settings.research_cache_days
                )
                for lead in leads:
                    note = await service.research_lead(
                        business_name=lead.business_name,
                        category=lead.category,
                        address=lead.address,
                        website=lead.website,
                    )
                    if note.usable:
                        await repo.set_research(lead.id, note.text, note.source_url)
                    out.append((lead.business_name, note.text))
        finally:
            await service.aclose()
        return out

    results = _run(_research())
    for name, note in results:
        console.print(f"[bold]{name}[/bold]: {note}")
    console.print(f"[green]{len(results)} leads researched[/green]")


# ---------------------------------------------------------------------------
# calls
# ---------------------------------------------------------------------------
@call_app.command("dial")
def call_dial(
    lead_id: str = typer.Argument(..., help="lead UUID"),
    dry_run: bool = typer.Option(False, help="create the call record but do not dial"),
) -> None:
    """Place one outbound call."""
    from app.db.base import session_scope
    from app.services.call_service import CallRejected, CallService

    async def _dial():  # noqa: ANN202
        async with session_scope() as session:
            return await CallService(session).dial(uuid.UUID(lead_id), dry_run=dry_run)

    try:
        result = _run(_dial())
    except CallRejected as exc:
        console.print(f"[red]call rejected:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print_json(json.dumps(result.as_dict()))


@call_app.command("simulate")
def call_simulate(
    lead_id: str = typer.Argument(..., help="lead UUID"),
    persona: str = typer.Option("interested_01", help="persona id to play"),
    provider: str = typer.Option(None, help="agent LLM provider (default: EVAL_LLM_PROVIDER)"),
) -> None:
    """Rehearse a call in text only, persisting the transcript and cost like a real call."""
    from app.db.base import create_all, session_scope
    from app.db.repositories import CallRepository
    from app.enums import CallOutcome
    from app.llm.router import build_client
    from app.pipeline.engine import ConversationEngine, LeadContext
    from app.pipeline.persistence import DbTurnSink
    from app.services.call_service import CallService
    from eval.personas import load_personas
    from eval.simulated_caller import ScriptedCaller

    async def _simulate() -> dict:
        settings = get_settings()
        if settings.is_sqlite:
            await create_all()
        chosen = next((p for p in load_personas() if p.id == persona), None)
        if chosen is None:
            raise typer.BadParameter(f"unknown persona '{persona}'")

        async with session_scope() as session:
            service = CallService(session)
            lead_ctx = await service.lead_context(uuid.UUID(lead_id))
            attempt = await CallRepository(session).attempt_number(uuid.UUID(lead_id))
            call = await CallRepository(session).create(uuid.UUID(lead_id), attempt_number=attempt)
            call_id = call.id

        llm = build_client(provider or settings.eval_llm_provider)
        assert llm is not None
        engine = ConversationEngine(
            lead=LeadContext(**{**lead_ctx.__dict__}),
            llm=llm,
            sink=DbTurnSink(call_id),
            call_id=call_id,
        )
        caller = ScriptedCaller(chosen)
        turn = await engine.start()
        console.print(f"[cyan]AGENT[/cyan] {turn.text}")
        for _ in range(settings.eval_max_turns):
            if engine.ended:
                break
            action = await caller.reply(turn.text, engine.state)
            if action.kind == "hangup":
                await engine.abort(CallOutcome.HUNG_UP, "lead hung up")
                console.print("[red]LEAD hung up[/red]")
                break
            if action.kind == "silence":
                turn = await engine.handle_silence()
                console.print("[dim]LEAD (silence)[/dim]")
                console.print(f"[cyan]AGENT[/cyan] {turn.text}")
                continue
            console.print(f"[magenta]LEAD[/magenta] {action.text}")
            turn = await engine.handle_user(action.text, action.confidence)
            label = f" [{turn.tool_name}]" if turn.tool_name else ""
            console.print(f"[cyan]AGENT[/cyan] {turn.text}[yellow]{label}[/yellow]")
        if not engine.ended:
            await engine.abort(CallOutcome.FAILED, "turn budget exhausted")
        await llm.aclose()

        async with session_scope() as session:
            return await CallService(session).finalize_from_engine(call_id, engine)

    metrics = _run(_simulate())
    console.print_json(json.dumps(metrics, default=str))


@call_app.command("show")
def call_show(call_id: str) -> None:
    """Print a call's transcript, tool calls and cost."""
    from app.db.base import session_scope
    from app.db.repositories import CallRepository, CostRepository

    async def _show():  # noqa: ANN202
        async with session_scope() as session:
            repo = CallRepository(session)
            call = await repo.get(uuid.UUID(call_id))
            turns = await repo.transcript(uuid.UUID(call_id))
            cost = await CostRepository(session).get(uuid.UUID(call_id))
            return call, turns, cost

    call, turns, cost = _run(_show())
    if call is None:
        console.print("[red]call not found[/red]")
        raise typer.Exit(code=1)
    console.print(f"[bold]{call.id}[/bold] outcome={call.outcome} state={call.final_state}")
    for turn in turns:
        tool = f"  [yellow][{turn.tool_called}][/yellow]" if turn.tool_called else ""
        console.print(f"[dim]{turn.turn_index:>2}[/dim] {turn.role.upper():5} {turn.content}{tool}")
    if cost:
        console.print(f"[green]cost[/green] ${float(cost.cost_usd):.4f}")


# ---------------------------------------------------------------------------
# campaign / costs / eval / flow
# ---------------------------------------------------------------------------
@campaign_app.command("run")
def campaign_run(limit: int = 10, concurrency: int = 2, dry_run: bool = False) -> None:
    """Dial every lead that is currently due."""
    from app.services.campaign import CampaignRunner

    report = _run(CampaignRunner(concurrency=concurrency, dry_run=dry_run).run(limit=limit))
    console.print_json(json.dumps(report.as_dict(), default=str))


@costs_app.command("report")
def costs_report(days: int = 7, markdown: bool = True) -> None:
    """Cost per call and cost per booked meeting."""
    from app.db.base import session_scope
    from app.services.reporting import build_summary, render_summary_markdown

    async def _summary():  # noqa: ANN202
        async with session_scope() as session:
            return await build_summary(session, days=days)

    summary = _run(_summary())
    if markdown:
        console.print(render_summary_markdown(summary))
    else:
        console.print_json(json.dumps(summary, default=str))


@eval_app.command("run")
def eval_run(
    label: str = "baseline",
    variant: str = typer.Option(None, help="prompt variant"),
    provider: str = typer.Option(None, help="agent LLM provider"),
    persist: bool = typer.Option(False, help="write results to the database"),
) -> None:
    """Run the offline persona suite."""
    from app.pipeline.prompts import PROMPT_VERSION
    from eval.run_eval import main as eval_main

    argv = ["--label", label, "--variant", variant or PROMPT_VERSION]
    if provider:
        argv += ["--provider", provider]
    if persist:
        argv += ["--persist"]
    raise typer.Exit(code=eval_main(argv))


@flow_app.command("show")
def flow_show() -> None:
    """Print the state machine and the tools callable from each state."""
    from app.enums import ConversationState
    from app.pipeline.flow import flow_summary
    from app.pipeline.states import successors

    table = Table(title="conversation state machine")
    table.add_column("state")
    table.add_column("tools")
    table.add_column("can transition to")
    summary = flow_summary()
    for state in ConversationState:
        table.add_row(
            state.value,
            ", ".join(summary[state.value]) or "-",
            ", ".join(sorted(s.value for s in successors(state))) or "-",
        )
    console.print(table)


if __name__ == "__main__":  # pragma: no cover
    app()
