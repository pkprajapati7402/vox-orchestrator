"""Eval harness: run every persona against the real conversation engine.

Text only, no audio — the point is to test *decisions* (state transitions, tool
choice, guard behaviour), and skipping TTS/STT makes the suite fast and free
enough to run on every pull request.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.costs.tracker import CostAccumulator
from app.enums import CallOutcome
from app.llm.base import LLMClient
from app.llm.router import build_client
from app.logging_config import get_logger
from app.pipeline.engine import ConversationEngine, LeadContext
from app.pipeline.prompts import PROMPT_VERSION
from eval.personas import Persona, load_personas
from eval.scoring import CaseResult, SuiteScore, aggregate, score_case
from eval.simulated_caller import LLMCaller, ScriptedCaller

log = get_logger(__name__)


@dataclass
class EvalReport:
    label: str
    prompt_variant: str
    agent_provider: str
    caller: str
    score: SuiteScore
    results: list[CaseResult] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    git_sha: str | None = None

    def as_dict(self, include_transcripts: bool = False) -> dict[str, Any]:
        return {
            "label": self.label,
            "prompt_variant": self.prompt_variant,
            "agent_provider": self.agent_provider,
            "caller": self.caller,
            "git_sha": self.git_sha,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "score": self.score.as_dict(),
            "results": [r.as_dict(include_transcripts) for r in self.results],
        }

    @property
    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]


def _git_sha() -> str | None:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except Exception:  # noqa: BLE001 - not a git checkout / git missing
        return None


def _lead_context(persona: Persona) -> LeadContext:
    business = persona.business
    return LeadContext(
        business_name=business.name,
        phone=business.phone,
        category=business.category,
        address=business.area,
        contact_name=business.contact_name,
        research_notes=business.research_notes,
    )


async def run_persona(
    persona: Persona,
    *,
    agent_llm: LLMClient,
    caller_llm: LLMClient | None = None,
    prompt_variant: str = PROMPT_VERSION,
    max_turns: int | None = None,
    keep_transcript: bool = True,
) -> CaseResult:
    """Play one persona against the engine and score the conversation."""
    settings = get_settings()
    max_turns = max_turns or settings.eval_max_turns

    engine = ConversationEngine(
        lead=_lead_context(persona),
        llm=agent_llm,
        prompt_variant=prompt_variant,
        cost=CostAccumulator(),
    )
    caller: ScriptedCaller | LLMCaller = (
        LLMCaller(persona, caller_llm) if caller_llm is not None else ScriptedCaller(persona)
    )

    if persona.behaviour.voicemail:
        # AMD fires before any conversation happens.
        await engine.handle_voicemail("answering machine greeting detected")
        return score_case(
            persona, engine.metrics(), engine.transcript_text() if keep_transcript else ""
        )

    agent_turn = await engine.start()
    agent_text = agent_turn.text

    for _ in range(max_turns):
        if engine.ended:
            break
        action = await caller.reply(agent_text, engine.state)

        if action.kind == "hangup":
            await engine.abort(CallOutcome.HUNG_UP, "lead hung up abruptly")
            break
        if action.kind == "silence":
            turn = await engine.handle_silence()
            agent_text = turn.text
            if turn.ended:
                break
            continue

        turn = await engine.handle_user(action.text, action.confidence)
        agent_text = turn.text
        if turn.ended:
            break

    if not engine.ended:
        await engine.abort(
            CallOutcome.FAILED, "conversation did not resolve within the turn budget"
        )

    return score_case(
        persona, engine.metrics(), engine.transcript_text() if keep_transcript else ""
    )


async def run_suite(
    personas: Sequence[Persona] | None = None,
    *,
    label: str = "baseline",
    prompt_variant: str = PROMPT_VERSION,
    agent_provider: str | None = None,
    caller_provider: str | None = None,
    concurrency: int = 8,
    keep_transcripts: bool = False,
) -> EvalReport:
    """Run the whole suite and aggregate the scores."""
    settings = get_settings()
    personas = list(personas if personas is not None else load_personas())
    provider = agent_provider or settings.eval_llm_provider

    started_at = datetime.now(UTC).isoformat()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: list[CaseResult] = []

    async def run_one(persona: Persona) -> None:
        async with semaphore:
            # A fresh client per persona keeps mock-client state isolated.
            agent_llm = build_client(provider)
            caller_llm = build_client(caller_provider) if caller_provider else None
            assert agent_llm is not None, f"unknown agent provider: {provider}"
            try:
                result = await run_persona(
                    persona,
                    agent_llm=agent_llm,
                    caller_llm=caller_llm,
                    prompt_variant=prompt_variant,
                    keep_transcript=keep_transcripts,
                )
            finally:
                await agent_llm.aclose()
                if caller_llm is not None:
                    await caller_llm.aclose()
            results.append(result)

    await asyncio.gather(*(run_one(persona) for persona in personas))
    results.sort(key=lambda r: r.persona_id)

    report = EvalReport(
        label=label,
        prompt_variant=prompt_variant,
        agent_provider=provider,
        caller="llm" if caller_provider else "scripted",
        score=aggregate(results),
        results=results,
        started_at=started_at,
        finished_at=datetime.now(UTC).isoformat(),
        git_sha=_git_sha(),
    )
    log.info(
        "eval.finished",
        label=label,
        personas=report.score.personas_run,
        task_completion=report.score.task_completion_rate,
        hallucination=report.score.hallucinated_tool_call_rate,
    )
    return report


__all__ = ["EvalReport", "run_persona", "run_suite"]
