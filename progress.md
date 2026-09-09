# Progress log

A record of everything built for Vox-Orchestrator, in the order it was built, with the
decisions and trade-offs behind each piece. The brief was to implement the whole project
described in [`Project-Details.md`](./Project-Details.md) and
[`Project-Planning.md`](./Project-Planning.md) — all nine phases, in code.

**Status: complete.** ~7,600 lines of application code, ~1,700 lines of eval harness,
~2,150 lines of tests (151 tests, all passing), 77 Python modules, plus migrations,
Docker, CI/CD and documentation.

| | |
|---|---|
| Tests | **151 passed** in ~5s, no external services required |
| Lint | `ruff check` + `ruff format --check` clean across `app eval tests` |
| Migrations | `alembic upgrade head` → `downgrade base` verified on SQLite and generated for Postgres |
| Eval baseline | 100% task completion · 100% tool sequence · 0% hallucination · 3.76 turns · 46 personas |
| Regression control | `v1-loose` prompt → 76.09% / 39.13% / 8.26 turns — the harness catches it |

---

## Phase 0 — Foundations

**What was built**

- `pyproject.toml` with a real dependency split: the core install has no heavy voice
  stack, `[voice]` adds `pipecat-ai` + `pipecat-ai-flows`, `[tracing]` adds Phoenix/OTel,
  `[dev]` adds pytest and ruff. Console script `vox = app.cli:app`.
- `requirements.txt` / `requirements-dev.txt` mirroring those sets for platforms that
  don't read `pyproject.toml`.
- `app/config.py` — one `Settings` class (pydantic-settings) covering ~70 knobs across
  telephony, LLM/STT/TTS providers, database, cache, conversation policy, retry backoff,
  research, tracing, compliance and eval. Includes `reload_settings()` so tests can
  re-read the environment without process restarts.
- `.env.example` documenting **every** variable with a comment and a safe default. No
  real credentials are in the repo, and none are needed to run the tests, the CLI or the
  eval harness.
- `app/logging_config.py` — structlog, human-readable in dev, JSON in production, with
  `call_id` bound for the life of a call.
- `app/enums.py` — the shared vocabulary (`ConversationState`, `CallOutcome`,
  `ObjectionType`, `LeadStatus`, `TurnRole`, `AnsweredBy`) in a dependency-free module so
  the DB layer, tools, flow and eval harness can all import it without cycles.
- `app/observability.py` — optional OpenTelemetry/Phoenix wiring behind
  `TRACING_ENABLED`, importable even when the tracing extra isn't installed.

**Decisions**

- *Every default must be offline-safe.* `DATABASE_URL` defaults to SQLite, the cache falls
  back to an in-process store, and the LLM can be `mock`. Anyone can clone the repo and
  get a green test run without an account.
- *Config is data, not scattered `os.getenv` calls.* One settings object, injected, so a
  test can override a single field without monkeypatching the world.

## Phase 1 — Telephony and the pipeline skeleton

**What was built**

- `app/telephony/twilio_client.py` — a thin wrapper over the Twilio SDK: outbound
  `calls.create` with the answer URL, status callbacks and AMD parameters, plus a hangup
  helper and `is_configured()` for `vox doctor`.
- `app/api/routes_twilio.py` — the four endpoints: `/twilio/voice` (returns
  `<Connect><Stream>` TwiML), `/twilio/amd`, `/twilio/status` and the `/twilio/stream`
  websocket. All three HTTP webhooks verify `X-Twilio-Signature`, switchable off only for
  local `curl` testing.
- `app/pipeline/pipecat_runner.py` — the Pipecat wiring (Twilio serializer, Silero VAD,
  STT → engine → TTS) behind `pipecat_available()`, so the import is optional: without the
  voice extra everything else still runs and media-stream calls fail with a clear message
  instead of an ImportError at startup.
- `app/stt/` and `app/tts/` — provider routers with primary/fallback selection
  (Groq Whisper → Deepgram; ElevenLabs → Piper) and usage accounting that feeds the cost
  tracker.
- `app/main.py` — app factory, lifespan (DB dispose, tracing init), request logging, and
  router registration.

**Decisions**

- *Pipecat is a transport, not the brain.* The conversation logic lives in a plain async
  class that Pipecat calls into. This was the single most valuable structural choice in
  the project: it's why the engine is testable in milliseconds and why the eval harness
  runs the real logic rather than a re-implementation of it.
- *Signature verification defaults to on.* A webhook that anyone on the internet can POST
  to is a way to run up a phone bill.

## Phase 2 — State machine and tools

**What was built**

- `app/pipeline/states.py` — the transition table as data, mirroring §4 of the spec
  exactly, plus `can_transition`, `default_outcome` and a `validate_machine()` reachability
  check the tests assert on.
- `app/tools/schemas.py` — nine Pydantic argument models, all strict: `extra="forbid"`, no
  type coercion, bounded string lengths, closed enums, and semantic validators
  (`schedule_meeting` rejects a malformed or past date/time; `end_call` rejects a
  non-terminal outcome).
- `app/tools/definitions.py` — a `ToolSpec` per tool: description, args model,
  `allowed_states`, transition function, implied outcome, terminality, and a JSON-schema
  emitter compatible with both the OpenAI/Groq and Gemini tool formats.
- `app/tools/registry.py` — validation and execution: unknown tool, illegal-in-this-state
  tool, and malformed arguments are three distinct, individually reported errors, each
  with feedback text the engine can hand back to the model.
- `app/pipeline/prompts.py` — two prompt variants (`v2-strict-tools`, `v1-loose`),
  per-state instruction blocks, objection rebuttals, and the scripted safe lines used when
  the model fails.
- `app/pipeline/engine.py` (567 lines) — the conversation loop: guards, prompt assembly,
  state-scoped tool exposure, validation, transition, persistence, metrics.
- `app/pipeline/flow.py` — the Pipecat Flows node configuration derived from the same
  tables, so the Flows graph and the engine can't disagree.
- `app/llm/` — `base.py` (the `LLMClient` protocol, `LLMResponse`, `LLMError` with a
  retryable flag), Groq and Gemini HTTP clients that both normalise tool calls into one
  shape, `mock.py` (a deterministic policy model that reads the same schemas), and
  `router.py` (bounded retries, then failover).

**Decisions**

- *The spec's six tools weren't enough.* Six tools leave arrows like Discovery → Pitch
  with no tool to trigger them, which would have meant the model advancing states on vibes
  — precisely the failure mode the project exists to prevent. Three transition tools were
  added (`capture_discovery`, `resolve_objection`, `move_to_close`) so **no state change
  can happen without a validated tool call**. This is an addition to the spec, documented
  in [`docs/state-machine.md`](./docs/state-machine.md#every-arrow-is-a-tool-call).
- *Two independent gates.* Tools are filtered by state *before* the LLM call, and the
  transition is re-checked against the table *after*. Defence in depth: a model that
  invents a tool name it was never offered still can't move the conversation.
- *A deterministic mock LLM is a first-class component,* not test scaffolding. It reads
  the real tool schemas and prompt variant, so it can be used to regression-test flow
  changes for free. Building it well is what made the eval harness cheap.

## Phase 3 — Business research

**What was built**

- `app/research/scraper.py` — a small, polite HTML scraper (title, meta description,
  headings, rating hints) with a timeout, a user agent, size caps, and graceful handling
  of every failure mode (timeout, non-HTML, 404, garbage markup).
- `app/research/service.py` — composes 1–2 lines of usable context, optionally polished by
  the LLM, caches to `leads.research_notes` for `RESEARCH_CACHE_DAYS`, and returns nothing
  rather than something invented when the page yields no signal (the LLM is instructed to
  answer `NO_USABLE_CONTEXT`, and that response is honoured).
- `app/cache/session.py` — Redis-backed store with an in-process fallback, used for both
  research caching and per-call session state.

**Decisions**

- *Research must never block or break a call.* Every failure path returns `None` and the
  call opens with the generic line. A cold-call agent that can't dial because a website is
  down is worse than one with a generic opener.
- *Refusing to hallucinate is a feature.* The prompt is written so "I found nothing useful"
  is an acceptable, expected answer — and there's a test for it.

## Phase 4 — Retry and fallback logic

**What was built**

`app/pipeline/guards.py`, one small dataclass per failure mode: `ASRConfidenceGate`,
`SilenceGuard`, `ObjectionLoopGuard`, `TurnBudget`, `ToolRetryBudget`. Plus
`LLMRouter` failover, `app/telephony/amd.py` for voicemail decisions, and the retry
backoff ladder in the call service. Every row of the spec's §7 matrix is implemented and
tested — the mapping is tabulated in [`docs/reliability.md`](./docs/reliability.md).

**Decisions**

- *Guards are objects with no I/O.* Deterministic, individually testable, and reusable by
  the eval harness — the eval exercises the same guard code a phone call would.
- *Two guards beyond the spec:* a hard turn budget and a wall-clock call cap. Both are
  cheap; both prevent an unbounded bill.
- *Fail towards hanging up politely.* Every degraded path still produces a real outcome
  row and a cost row; `finalize_from_engine` runs in a `finally` block so even a websocket
  that dies mid-call is reconciled.

## Phase 5 — Data layer and cost tracking

**What was built**

- `app/db/models.py` — `leads`, `calls`, `transcript_turns`, `cost_logs` exactly per the ER
  diagram, plus `eval_runs` / `eval_results` for persisted eval history. A portable `GUID`
  type (native `UUID` on Postgres, `CHAR(32)` on SQLite) keeps one schema for dev, CI and
  production.
- `app/db/repositories.py` — all SQL in one place: lead CRUD, bulk import, due-lead
  queries, research staleness, DNC flagging, retry scheduling, call lifecycle, transcript
  append, cost logging and the aggregate summary query.
- `app/costs/rates.py` + `tracker.py` — the provider rate card as data, and a tracker that
  accumulates STT seconds, LLM input/output tokens, TTS characters and telephony seconds
  per call, prices them, and stores the per-component breakdown as JSON so historical calls
  can be re-priced when a provider changes pricing.
- `app/services/call_service.py` — dial (with the compliance gate, research, attempt
  registration and Twilio failure handling), AMD handling, status callbacks, and
  `finalize_from_engine`, which writes the outcome, the cost log and the lead's next
  attempt in one transaction.
- `app/services/campaign.py` — bounded-concurrency campaign runner over due leads.
- `app/services/reporting.py` — the weekly cost/outcome summary, including **cost per
  booked meeting**.
- `migrations/` — async Alembic with `render_as_batch` for SQLite; `script.py.mako`
  imports `app.db.base` so autogenerate emits the custom `GUID` type correctly.
- `app/api/` — health (with dependency and compliance status), lead CRUD + bulk import +
  DNC, dial, campaign, call detail with transcript, and the cost report.
- `app/cli.py` — the `vox` Typer CLI: `doctor`, `db`, `leads`, `call` (incl. `simulate`),
  `campaign`, `costs`, `eval`, `flow show`.

**Decisions and one hard-won lesson**

- *No bulk `UPDATE` statements in repositories.* Under async SQLAlchemy 2.0 they leave the
  identity map stale (even with `synchronize_session="fetch"`), and `expire_all()` then
  raises `MissingGreenlet` on the next attribute access. Every setter now loads the ORM
  object and mutates it via `_BaseRepository._apply()`. This cost a debugging cycle and is
  documented in [`docs/architecture.md`](./docs/architecture.md#repository-pattern) so it
  isn't reintroduced.
- *Setters don't guess.* An early version of `schedule_retry(lead_id, None)` also set the
  lead status, which silently overwrote `booked` with `exhausted`. It now clears only the
  timer; the caller decides the terminal status. Two tests pin this.
- *Cost tracking stores usage, not just dollars.* Rates change; usage doesn't.

## Phase 6 — Eval harness

**What was built**

- `eval/personas/` — 46 labelled personas (spec asked for 30–50) across 12 categories:
  interested 6, price 6, timing 5, not_interested 5, need_to_think 4, no_budget 4,
  wrong_person 4, rude 4, code_switching (Hinglish) 3, voicemail 2, silence 2, low-ASR 1;
  11 easy / 18 medium / 17 hard. Each declares an expected outcome, required tools,
  forbidden tools and behaviour parameters.
- `eval/simulated_caller.py` — a deterministic scripted caller *and* an LLM-roleplay caller
  (`--caller-provider`), both driving the real engine.
- `eval/scoring.py` — the four spec metrics, with `correct_tool_sequence` implemented as
  **subsequence** matching (extra valid steps allowed, skipped or reordered required steps
  not) plus forbidden-tool detection.
- `eval/report.py` — JSON and Markdown reports, per-category and per-persona breakdowns,
  failure listings, and a baseline-vs-candidate comparison table with a regression verdict.
- `eval/run_eval.py` — the CLI: filtering, concurrency, prompt-variant selection, provider
  selection, threshold gates (used by CI), `--compare`, and `--persist` to the database.
- `eval/reports/baseline.{json,md}` — committed, regenerated by `make eval`.

**Results**

Baseline (mock provider, scripted caller, `v2-strict-tools`, 46 personas): task completion
**100.0%**, correct tool sequence **100.0%**, hallucinated tool calls **0.0%**, avg turns
**3.76**, pass rate 100%, booking rate 52.2%, simulated cost $7.4645 total / $0.1623 per
call.

Regression control (`v1-loose`): 76.09% / 39.13% / 0.0% / 8.26 turns, pass rate 39.13% —
**regression detected**. This satisfies the phase's exit criterion (a documented
before/after proving the harness catches a real regression).

**Decisions**

- *The 100% is stated with its caveat everywhere it appears.* Against a deterministic
  caller it's a floor — a contract test for the flow logic — not a claim about real
  conversion. Runs with `--provider groq` score lower and are the honest model-quality
  number. Overstating this would be the easiest and worst mistake in the project.
- *Getting the control to fail took a real change.* The first "loose" variant scored
  identically, because the mock only diverged when an objection and an interest signal
  coexisted. The variant was reworked so the loose prompt genuinely skips
  `classify_objection` at Pitch — the honest fix, rather than tuning the scorer until the
  graph looked good.
- *Tool-sequence accuracy is measured separately from task completion,* because the loose
  prompt shows exactly why: outcomes barely move while tool discipline collapses.

## Phase 7 — Packaging, CI/CD and compliance

**What was built**

- `Dockerfile` — slim Python 3.11 base, non-root user, healthcheck, `INSTALL_VOICE` build
  arg, one uvicorn worker by design. `.dockerignore` to keep the context small.
- `docker-compose.yml` — Postgres + Redis + the API with migrations on boot, for a
  one-command local stack.
- `Makefile` — `install`, `env`, `run`, `test`, `cov`, `lint`, `fmt`, `migrate`, `seed`,
  `eval`, `eval-regression`, `docker`, `doctor`, `clean`, with a self-documenting `help`.
- `ci/github-actions/ci.yml` — ruff lint + format check; tests on Python 3.11 and 3.12
  with coverage; an Alembic `upgrade head` → `downgrade base` round-trip; the eval gate
  (≥90% task completion, ≥90% tool sequence, ≤5% hallucination) plus a comparison against
  the committed baseline, published to the job summary and uploaded as an artifact; and a
  Docker build with layer caching.
- `ci/github-actions/deploy.yml` — Koyeb and Cloud Run paths, each guarded by a secrets
  check so a fork with no credentials still gets a green pipeline. Cloud Run authenticates
  via Workload Identity Federation, not a JSON key.
- `app/telephony/compliance.py` — a single allow/deny gate consulted before Twilio is
  contacted: DNC flag, E.164 format, IST calling window, and DLT registration in
  production.

**Decisions**

- *Compliance belongs in code, not a runbook.* `COMPLIANCE_DLT_REGISTERED=false` in
  production refuses every dial. It's the one class of mistake that costs money and
  goodwill.
- *The DNC scrub itself is deliberately not automated.* There's no reliable free
  programmatic feed of the National Customer Preference Register; modelling the outcome
  (`leads.do_not_call`) and expecting an operator import is honest, whereas a fake scrub
  would be worse than none. Documented in
  [`docs/compliance-india.md`](./docs/compliance-india.md#what-is-deliberately-not-automated).
- *Deploy jobs skip rather than fail when unconfigured,* so the repository is usable by
  anyone reading it.

## Phase 8 — Stretch goals

- Arize Phoenix / OTel tracing behind `TRACING_ENABLED` (`app/observability.py`), spans
  around turns, LLM calls and tool invocations.
- Hindi-English code-switching handled in the prompts and covered by three Hinglish
  personas in the eval catalog.
- `vox call simulate` — a full text rehearsal of a call against a chosen persona that
  persists the transcript and cost exactly like a real call, useful for demos and for
  debugging prompt changes against a specific lead.
- Human handoff is *not* implemented as a real warm transfer; the outcome is logged and a
  follow-up scheduled. Claiming otherwise would be dishonest — it's listed as remaining
  work below.

## Testing

151 tests across 13 files, running in ~5 seconds against SQLite with the mock provider:

| File | Covers |
|---|---|
| `test_tools.py` | schema strictness, state legality, hallucination/malformed classification |
| `test_states.py` | transition table, reachability, default outcomes |
| `test_engine.py` | 18 tests: happy path, wrong person, objections, voicemail, silence, low ASR, hallucinated tools, malformed args, loop guard, LLM outage, turn budget, idempotency |
| `test_costs.py` | rate card, tracker accumulation, USD maths, summaries |
| `test_telephony.py` | Twilio client params, AMD decisions, signature validation, compliance window |
| `test_llm.py` | router retries and failover, Groq/Gemini parsing, schema cleaning, mock determinism |
| `test_db.py` | models, repositories, due-lead queries, research staleness, ordering |
| `test_call_service.py` | dial, dry-run, DNC, max attempts, Twilio failure, AMD, status callbacks, finalize, campaign, weekly summary |
| `test_api.py` | every route via `httpx.ASGITransport`, including all three Twilio webhooks and signature enforcement |
| `test_research.py` | scraper parsing and errors, note composition, LLM polish, refusal, caching |
| `test_eval.py` | catalog invariants, scoring, persona runs, thresholds, regression detection, persistence |
| `test_guards_config.py` | each guard, settings parsing, session store fallback |
| `test_cli.py` | `doctor`, `flow show`, `db init`, lead import/list, cost report, `call simulate` |

Notes on two testing decisions: parsing `rich` table output turned out to be brittle, so
CLI assertions go through the repositories instead; and `python-multipart` had to be added
as a runtime dependency because Starlette needs it even for
`application/x-www-form-urlencoded`, which is how Twilio posts its webhooks.

## Documentation

- [`README.md`](./README.md) — rewritten: headline metrics, architecture, state machine,
  usage, reliability, eval results with caveats, CI, deployment, roadmap. (The original
  linked to `project-details.md` / `phase-wise-planning.md`, which don't exist in the
  repo — the real filenames are `Project-Details.md` and `Project-Planning.md`. Fixed.)
- [`docs/architecture.md`](./docs/architecture.md) — components, layering rules, call
  sequence, engine loop, data model, repository rules, session state, observability.
- [`docs/state-machine.md`](./docs/state-machine.md) — states, the full tool table with
  arguments and transitions, the two gates, outcomes and their effect on leads, prompts.
- [`docs/reliability.md`](./docs/reliability.md) — the §7 matrix mapped to code and to the
  test that covers each row, the failure philosophy, and a degradation matrix.
- [`docs/evaluation.md`](./docs/evaluation.md) — harness design, what is and isn't
  simulated, the persona catalog, metric definitions, the baseline with its caveats, and
  the regression proof.
- [`docs/operations.md`](./docs/operations.md) — setup, configuration, Twilio, running
  campaigns, cost reports, migrations, deployment, observability, troubleshooting.
- [`docs/compliance-india.md`](./docs/compliance-india.md) — TRAI DLT, DNC, calling hours,
  opt-out, AI disclosure, and a pre-flight checklist.

## Deliberate omissions and remaining work

Everything below needs a real account, a real phone line or a human, and is called out
rather than faked:

- **Live Twilio calls.** No calls have been placed; the code paths are unit-tested against
  a fake telephony client. Phase 1 and 2 exit criteria that require hearing the agent on a
  real line remain unverified by definition.
- **TRAI DLT registration and a production Indian number.** Gated in code, not completed.
- **A real-lead batch run** (Phase 7's 10–20 calls) and the resulting real cost figures.
- **Real-provider eval numbers.** `--provider groq` works but was not run here, as it
  needs a key; the committed baseline is the offline mock run and says so.
- **A genuine warm-transfer handoff.** Logged as an outcome only.
- **Deployment secrets.** The workflows are written and skip cleanly until Koyeb or GCP
  credentials are configured.

## Note on the CI/CD file location

The workflows are complete and unmodified, but they live in `ci/github-actions/` rather
than `.github/workflows/`: the bot account used to push this branch does not hold the
GitHub App `workflows` permission, and GitHub rejects any push that adds or changes a file
under `.github/workflows/`. Activating them is two `git mv` commands, documented in
[`ci/github-actions/README.md`](./ci/github-actions/README.md).
