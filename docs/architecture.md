# Architecture

How the pieces fit together, and what happens on a single call from `POST /calls/dial`
to a finalised row in `cost_logs`.

## Component map

```mermaid
flowchart TD
    CLI[vox CLI] --> API
    OPS[REST client] --> API
    API[FastAPI app<br/>app/main.py]

    API --> CS[CallService<br/>app/services/call_service.py]
    CS --> COMP[Compliance gate<br/>app/telephony/compliance.py]
    CS --> RS[ResearchService<br/>app/research/]
    CS --> TW[TwilioClient<br/>app/telephony/twilio_client.py]
    RS --> CACHE[(Redis / in-process cache)]

    TW --> TWILIO((Twilio Voice))
    TWILIO -->|POST /twilio/voice| API
    TWILIO -->|POST /twilio/amd, /twilio/status| API
    TWILIO <-->|WS /twilio/stream| PIPE

    subgraph PIPE[Per-call pipeline · app/pipeline/]
        STT[STT frames] --> ENG[ConversationEngine<br/>engine.py]
        ENG --> REG[Tool registry<br/>app/tools/registry.py]
        REG --> SM[State machine<br/>states.py]
        ENG --> LLM[LLM router<br/>app/llm/router.py]
        ENG --> TTS[TTS frames]
    end

    ENG --> SINK[DbTurnSink<br/>pipeline/persistence.py]
    SINK --> DB[(Postgres)]
    CS --> DB
    CT[CostTracker<br/>app/costs/tracker.py] --> DB
    EVAL[eval/ harness] --> ENG
    ENG -.OTel spans.-> PHX[Arize Phoenix]
```

## Layering rules

The codebase is deliberately layered so the conversation logic can be tested without
Twilio, without a provider key and without Postgres.

| Layer | Modules | Knows about |
|---|---|---|
| Domain | `app/enums.py`, `app/pipeline/states.py`, `app/tools/` | nothing else |
| Conversation | `app/pipeline/engine.py`, `guards.py`, `prompts.py`, `flow.py` | domain + an LLM *protocol* |
| Providers | `app/llm/`, `app/stt/`, `app/tts/`, `app/telephony/`, `app/research/` | HTTP + config |
| Persistence | `app/db/` | SQLAlchemy models and repositories only |
| Services | `app/services/` | all of the above, orchestrates them |
| Edges | `app/api/`, `app/cli.py`, `eval/` | services |

`ConversationEngine` depends on the `LLMClient` protocol (`app/llm/base.py`), never on a
concrete provider. That single seam is what lets the eval harness drive the *real* flow
logic with a deterministic mock model, and lets the Pipecat runner drive it with Groq.

## Anatomy of one call

```mermaid
sequenceDiagram
    autonumber
    participant Op as CLI / API
    participant CS as CallService
    participant DB as Postgres
    participant TW as Twilio
    participant WS as /twilio/stream
    participant EN as ConversationEngine

    Op->>CS: dial(lead_id)
    CS->>DB: load lead, check do_not_call + calling window
    CS->>CS: research (cached ≤ RESEARCH_CACHE_DAYS)
    CS->>DB: INSERT calls (status=dialing)
    CS->>TW: calls.create(url=/twilio/voice?call_id=…, AMD on)
    TW-->>CS: CallSid
    CS->>DB: UPDATE calls.provider_call_sid

    TW->>WS: POST /twilio/voice
    WS-->>TW: TwiML <Connect><Stream url=wss://…/twilio/stream>
    TW->>WS: websocket "start" (customParameters.call_id)
    WS->>EN: build_session(lead_context)
    loop every caller utterance
        TW->>WS: media frames -> STT text + confidence
        WS->>EN: handle_user_turn(text, confidence)
        EN->>EN: guards (ASR / silence / objection loop / turn budget)
        EN->>EN: LLM completion with the tools legal in this state
        EN->>EN: validate tool args, check state transition
        EN-->>WS: agent line -> TTS -> media frames
        EN->>DB: transcript_turns (via DbTurnSink)
    end
    TW->>WS: POST /twilio/amd  (parallel, may hang up as voicemail)
    TW->>WS: POST /twilio/status (completed / no-answer / busy)
    WS->>CS: finalize_from_engine(call_id, engine)
    CS->>DB: calls.outcome, cost_logs row, lead status + next_attempt_at
```

## The engine loop

`ConversationEngine.handle_user_turn()` is the heart of the system. Each turn:

1. **Guards run first.** Silence, low ASR confidence, objection cycles and the global
   turn budget can short-circuit the turn before any token is spent
   (`app/pipeline/guards.py`).
2. **Prompt assembly.** `prompts.py` builds a system prompt from the lead context,
   research notes, the current state and the state-specific instruction block.
3. **Tool exposure is state-scoped.** Only tools whose `allowed_states` contain the
   current state are sent to the model — a hallucinated `schedule_meeting` during
   Greeting is not just rejected, it was never offered.
4. **Validation.** Arguments are parsed by a Pydantic model
   (`app/tools/schemas.py`). A `ValidationError` is fed back to the model once
   (`MAX_TOOL_VALIDATION_RETRIES`), then the engine falls back to a scripted safe line.
5. **Transition.** The tool's `transition` function returns the target state; the engine
   refuses it unless `states.can_transition(current, target)` agrees.
6. **Persistence.** Every turn (role, text, state, tool name, tool args, latency,
   ASR confidence) is written through the turn sink.

The engine is pure `async` Python with no Pipecat import, so `tests/test_engine.py`
exercises the whole loop in milliseconds.

## Data model

```mermaid
erDiagram
    LEADS ||--o{ CALLS : "has"
    CALLS ||--o{ TRANSCRIPT_TURNS : "has"
    CALLS ||--|| COST_LOGS : "has"
    EVAL_RUNS ||--o{ EVAL_RESULTS : "has"

    LEADS {
        uuid id PK
        string business_name
        string phone
        string category
        string website
        text research_notes
        timestamp research_fetched_at
        string status
        int attempt_count
        timestamp next_attempt_at
        bool do_not_call
    }
    CALLS {
        uuid id PK
        uuid lead_id FK
        string provider_call_sid
        string status
        string outcome
        string answered_by
        text summary
        timestamp started_at
        timestamp ended_at
        int duration_seconds
    }
    TRANSCRIPT_TURNS {
        uuid id PK
        uuid call_id FK
        int turn_index
        string role
        text content
        string state
        string tool_name
        json tool_args
        float asr_confidence
        int latency_ms
    }
    COST_LOGS {
        uuid id PK
        uuid call_id FK
        float stt_seconds
        int llm_input_tokens
        int llm_output_tokens
        int tts_characters
        float telephony_seconds
        float cost_usd
        json breakdown
    }
    EVAL_RESULTS {
        uuid id PK
        uuid run_id FK
        string persona_id
        string expected_outcome
        string actual_outcome
        bool task_completed
        bool correct_tool_sequence
        int hallucinated_tool_calls
        int turns_to_resolution
    }
```

`app/db/models.py` uses a portable `GUID` type (native `UUID` on Postgres, `CHAR(32)` on
SQLite) so the same schema and the same migrations run in CI and in production.

### Repository pattern

All SQL lives in `app/db/repositories.py`. Two rules, both learned the hard way:

* **No bulk `UPDATE` statements.** Under async SQLAlchemy 2.0 they leave the identity map
  stale, and `expire_all()` then explodes with `MissingGreenlet` on the next attribute
  access. Every setter loads the ORM object and mutates it (`_BaseRepository._apply`).
* **Setters do not guess.** `schedule_retry(lead_id, None)` only clears the timer; the
  caller decides whether the lead is `booked`, `rejected` or `exhausted`.

## Session state

Per-call conversation state lives in memory for the life of the websocket and is mirrored
into Redis (`app/cache/session.py`) when `REDIS_URL` is set, so a restart mid-campaign
doesn't lose the state of calls that are still connected. With no Redis configured the
module falls back to an in-process dict — correct for a single worker, which is exactly
how the container is meant to be scaled (one worker per instance, more instances).

## Observability

`app/observability.py` configures OpenTelemetry when `TRACING_ENABLED=true`, exporting to
an Arize Phoenix collector. Spans wrap each turn, each LLM call and each tool invocation.
Logging is `structlog`, human-readable in development and JSON in production
(`LOG_JSON=true`), with `call_id` bound to every line emitted during a call.
