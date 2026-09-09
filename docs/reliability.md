# Reliability engineering

The retry/fallback matrix from Project-Details.md §7, and where each row lives in code.
Every row has a test.

| Failure mode | Handling | Implementation | Test |
|---|---|---|---|
| Low ASR confidence / unclear speech | Ask the caller to repeat **once** (`STT_MIN_CONFIDENCE`, `MAX_ASR_REPEAT_REQUESTS`), then proceed with the best-guess transcript rather than looping | `ASRConfidenceGate` (`app/pipeline/guards.py`) + `ConversationEngine.handle_user_turn` | `test_guards_config.py`, `test_engine.py::test_low_asr_confidence_asks_for_a_repeat_once` |
| Voicemail detected (Twilio AMD) | No pitch: `log_voicemail` → outcome `voicemail`, call hung up, lead retried on the backoff schedule | `POST /twilio/amd` (`app/api/routes_twilio.py`), `app/telephony/amd.py`, `CallService.handle_amd` | `test_telephony.py`, `test_call_service.py::test_amd_voicemail_hangs_up_logs_and_retries`, `test_api.py` |
| Malformed / unknown / illegal tool call | Re-prompt once with the validation error appended to context (`MAX_TOOL_VALIDATION_RETRIES`); on a second failure emit a scripted safe line and keep the state unchanged | `ToolRetryBudget` + `validate_tool_call` / `execute_tool` (`app/tools/registry.py`) | `test_tools.py`, `test_engine.py::test_hallucinated_tool_call_is_retried_then_falls_back` |
| LLM timeout / outage | `LLM_MAX_RETRIES` retries with exponential backoff against the primary, then failover to `LLM_FALLBACK_PROVIDER`; failover is counted in `router.stats` | `LLMRouter` (`app/llm/router.py`) | `test_llm.py::test_router_fails_over_to_the_secondary_provider` |
| Silence / no response | Prompt once ("Are you still there?"), then end the call gracefully with outcome `no_answer` | `SilenceGuard` | `test_engine.py::test_silence_prompts_once_then_hangs_up` |
| More than 3 objection cycles | Force a transition to Close; if the loop continues, force EndCall — the model does not get a vote | `ObjectionLoopGuard` (`MAX_OBJECTION_CYCLES`) | `test_engine.py::test_objection_loop_guard_forces_close_then_end` |

Two guards beyond the spec, because both are cheap and both are real:

* **`TurnBudget`** (`MAX_CONVERSATION_TURNS`, default 40) — a hard stop on conversation
  length regardless of state, so no bug can bill an unbounded call.
* **`TWILIO_MAX_CALL_SECONDS`** (default 420) — a wall-clock cap enforced at the Twilio
  layer, which survives even a hung Python process.

## Failure philosophy

**Fail towards hanging up politely.** Every degraded path terminates in a real outcome
row rather than an exception: a failed LLM failover ends the call as `failed`, an
unparseable tool call ends as a scripted line, and a websocket that dies mid-call is
still finalised, because `finalize_from_engine` runs in the `finally` block of the stream
handler. There is no path where a call ends without a `calls` row and a `cost_logs` row.

**Guards are objects, not `if` statements.** Each one is a small dataclass with no LLM,
no I/O and no config lookup at call time, which is why the entire matrix is testable in
milliseconds and why the eval harness exercises the same guard instances the phone call
would.

**Bounded everything.** Retries, repeats, objection cycles, turns, call seconds and
research timeouts all have an explicit ceiling in `app/config.py`. Nothing in the system
is allowed to loop on a caller's behalf.

## Degradation matrix

What still works when a dependency is down:

| Missing | Effect |
|---|---|
| `REDIS_URL` | In-process session store; correct for one worker, sessions lost on restart |
| Postgres | Fatal for calling; `/health` returns `status: degraded` with the DB error and dialing fails loudly |
| Groq | Automatic failover to Gemini, logged as `llm.failover` |
| Both LLM providers | Call ends with a scripted apology line and outcome `failed` |
| ElevenLabs | Piper (self-hosted, $0) if `TTS_PRIMARY_PROVIDER=piper` or the primary errors |
| Research (network/scrape failure) | Call proceeds with a generic opener; never blocks the dial |
| `pipecat` not installed | Media-stream calls are refused with a clear error; the API, CLI, eval harness and tests all still run |

## Compliance guardrails

Dialing is gated *before* Twilio is contacted (`app/telephony/compliance.py`):

* `do_not_call` on the lead → refused (`409` from the API, `CallRejected` in the service).
* Outside the calling window (`CALLING_WINDOW_START_HOUR`..`CALLING_WINDOW_END_HOUR`
  in `CALLING_TIMEZONE`) → refused.
* `COMPLIANCE_DLT_REGISTERED=false` in `APP_ENV=production` → refused.

See [`compliance-india.md`](./compliance-india.md).
