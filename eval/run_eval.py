#!/usr/bin/env python
"""Run the eval suite and write a JSON + Markdown report.

Examples
--------
    # baseline, offline, deterministic
    python -m eval.run_eval --label baseline

    # the deliberate prompt regression, compared against the baseline
    python -m eval.run_eval --label loose-prompt --variant v1-loose \\
        --compare eval/reports/baseline.json

    # real model in the loop (needs GROQ_API_KEY)
    python -m eval.run_eval --label groq-run --provider groq --caller-provider groq

    # CI gate
    python -m eval.run_eval --min-task-completion 0.85 --max-hallucination 0.05
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.config import get_settings
from app.logging_config import configure_logging
from app.pipeline.prompts import PROMPT_VARIANTS, PROMPT_VERSION
from eval.harness import run_suite
from eval.personas import load_personas
from eval.report import render_comparison, render_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the cold-call agent eval suite.")
    parser.add_argument("--label", default="baseline", help="name for this run")
    parser.add_argument(
        "--variant",
        default=PROMPT_VERSION,
        choices=sorted(PROMPT_VARIANTS),
        help="system-prompt variant under test",
    )
    parser.add_argument(
        "--provider", default=None, help="agent LLM provider (mock|groq|gemini); default from env"
    )
    parser.add_argument(
        "--caller-provider",
        default=None,
        help="LLM that plays the persona; omit for the deterministic scripted caller",
    )
    parser.add_argument("--personas", default=None, help="path to a persona JSON override file")
    parser.add_argument("--category", default=None, help="only run personas in this category")
    parser.add_argument("--limit", type=int, default=None, help="cap the number of personas")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument(
        "--out-dir", default=None, help="report directory (default: EVAL_REPORT_DIR)"
    )
    parser.add_argument("--transcripts", action="store_true", help="embed transcripts in the JSON")
    parser.add_argument("--compare", default=None, help="baseline JSON report to diff against")
    parser.add_argument("--persist", action="store_true", help="also write results to Postgres")
    parser.add_argument("--min-task-completion", type=float, default=None)
    parser.add_argument("--min-tool-sequence", type=float, default=None)
    parser.add_argument("--max-hallucination", type=float, default=None)
    parser.add_argument("--quiet", action="store_true")
    return parser


async def persist_report(report) -> None:  # noqa: ANN001
    """Store the run + per-persona results in the database."""
    from app.db.base import create_all, session_scope
    from app.db.repositories import EvalRepository

    settings = get_settings()
    if settings.is_sqlite:
        await create_all()
    async with session_scope() as session:
        repo = EvalRepository(session)
        run = await repo.create_run(
            label=report.label,
            git_sha=report.git_sha,
            prompt_version=report.prompt_variant,
            llm_provider=report.agent_provider,
            personas_run=report.score.personas_run,
            task_completion_rate=report.score.task_completion_rate,
            correct_tool_sequence_rate=report.score.correct_tool_sequence_rate,
            hallucinated_tool_call_rate=report.score.hallucinated_tool_call_rate,
            avg_turns_to_resolution=report.score.avg_turns_to_resolution,
        )
        for result in report.results:
            await repo.add_result(
                run.id,
                persona=result.persona_id,
                persona_category=result.category,
                expected_outcome=result.expected_outcome,
                actual_outcome=result.actual_outcome,
                task_completed=result.task_completed,
                correct_tool_sequence=result.correct_tool_sequence,
                hallucinated_tool_call=result.hallucinated_tool_call,
                turns_to_resolution=result.turns_to_resolution,
                final_state=result.final_state,
                tool_sequence=result.tool_sequence,
                failure_notes="; ".join(result.failure_notes) or None,
                cost_usd=result.cost_usd,
            )


async def main_async(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    settings = get_settings()

    personas = load_personas(args.personas)
    if args.category:
        personas = [p for p in personas if p.category == args.category]
    if args.limit:
        personas = personas[: args.limit]
    if not personas:
        print("no personas selected", file=sys.stderr)
        return 2

    report = await run_suite(
        personas,
        label=args.label,
        prompt_variant=args.variant,
        agent_provider=args.provider,
        caller_provider=args.caller_provider,
        concurrency=args.concurrency,
        keep_transcripts=args.transcripts,
    )

    out_dir = Path(args.out_dir or settings.eval_report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{args.label}.json"
    md_path = out_dir / f"{args.label}.md"
    json_path.write_text(json.dumps(report.as_dict(args.transcripts), indent=2))
    md_path.write_text(render_report(report))

    if args.persist:
        await persist_report(report)

    score = report.score
    if not args.quiet:
        print(render_report(report))
    print(f"\nreports written: {json_path}  {md_path}")

    exit_code = 0
    if args.compare:
        baseline = json.loads(Path(args.compare).read_text())
        comparison = render_comparison(baseline, report.as_dict())
        (out_dir / f"{args.label}-vs-baseline.md").write_text(comparison)
        print("\n" + comparison)

    if (
        args.min_task_completion is not None
        and score.task_completion_rate < args.min_task_completion
    ):
        print(
            f"FAIL: task completion {score.task_completion_rate:.2%} < "
            f"{args.min_task_completion:.2%}",
            file=sys.stderr,
        )
        exit_code = 1
    if (
        args.min_tool_sequence is not None
        and score.correct_tool_sequence_rate < args.min_tool_sequence
    ):
        print(
            f"FAIL: correct tool sequence {score.correct_tool_sequence_rate:.2%} < "
            f"{args.min_tool_sequence:.2%}",
            file=sys.stderr,
        )
        exit_code = 1
    if (
        args.max_hallucination is not None
        and score.hallucinated_tool_call_rate > args.max_hallucination
    ):
        print(
            f"FAIL: hallucination rate {score.hallucinated_tool_call_rate:.2%} > "
            f"{args.max_hallucination:.2%}",
            file=sys.stderr,
        )
        exit_code = 1
    return exit_code


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    raise SystemExit(main())
