# Operations runbook

Everything needed to take the repo from `git clone` to a real phone ringing, plus the
day-two commands.

## 1. Local setup

```bash
git clone https://github.com/Krypto-etox/vox-orchestrator.git
cd vox-orchestrator
make install          # venv + runtime + dev deps
make env              # cp .env.example .env
make db-init seed     # SQLite tables + the sample South Delhi lead list
make test             # 151 tests, no external services required
make run              # http://localhost:8000/docs
```

Nothing above needs an API key, a database server or Twilio. The defaults are SQLite, an
in-process cache and the deterministic mock LLM, which is what makes the test suite and
the eval harness runnable on a laptop and in CI.

To exercise the actual voice stack, add `make install-voice` (installs
`pipecat-ai` + `pipecat-ai-flows`). Without it, everything works except the
`/twilio/stream` websocket, which refuses the call with a clear error.

## 2. Configuration

Every setting is an environment variable read by `app/config.py` (pydantic-settings) and
documented in `.env.example`. The ones that actually gate behaviour:

| Variable | Why it matters |
|---|---|
| `PUBLIC_BASE_URL` | The URL Twilio calls back. Wrong value = silent call failures. No trailing slash. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./vox_orchestrator.sqlite3` for dev, `postgresql+asyncpg://…` in production |
| `LLM_PRIMARY_PROVIDER` / `LLM_FALLBACK_PROVIDER` | `groq` → `gemini` by default; set both to `mock` for offline work |
| `TWILIO_VALIDATE_SIGNATURES` | Keep `true` everywhere except local `curl` testing |
| `COMPLIANCE_DLT_REGISTERED` | Must be `true` before `APP_ENV=production` will dial at all |
| `CALLING_WINDOW_START_HOUR` / `_END_HOUR` | IST window outside which dialing is refused |
| `CALL_RETRY_BACKOFF_MINUTES` | `60,240,1440` — the retry ladder for unanswered calls |

`vox doctor` prints which integrations are configured and which are missing — run it
first whenever something behaves unexpectedly.

## 3. Twilio setup

1. Buy (or use the trial) voice-capable number; put it in `TWILIO_PHONE_NUMBER` in E.164.
2. On a trial account, **verify every destination number** you intend to call
   (Console → Phone Numbers → Verified Caller IDs). Trial accounts cannot dial anything
   else, and the failure looks like an instant `failed` status callback.
3. Expose the local API: `make tunnel` (ngrok) and set `PUBLIC_BASE_URL` to the https URL.
4. No console webhook configuration is needed for outbound: `CallService.dial` passes
   `url`, `status_callback` and `machine_detection` per call. The number's console
   webhooks only matter for *inbound* calls.

Twilio hits four endpoints:

| Endpoint | Purpose |
|---|---|
| `POST /twilio/voice` | returns TwiML: `<Connect><Stream url="wss://…/twilio/stream">` |
| `WS /twilio/stream` | bidirectional µ-law audio, 8 kHz, base64 frames |
| `POST /twilio/amd` | async answering-machine detection result |
| `POST /twilio/status` | `initiated` / `ringing` / `answered` / `completed` / `no-answer` / `busy` / `failed` |

All three HTTP webhooks verify `X-Twilio-Signature` against `PUBLIC_BASE_URL` + the exact
path. If verification fails you get `403` — a mismatch between `PUBLIC_BASE_URL` and the
URL Twilio actually called (http vs https, trailing slash, stale ngrok domain) is the
usual cause.

## 4. Placing calls

```bash
vox leads import data/sample_leads.csv     # business_name, phone, category, address, website
vox leads research --limit 20              # pre-call research, cached RESEARCH_CACHE_DAYS
vox call dial <lead-id>                    # one call
vox call dial <lead-id> --dry-run          # everything except talking to Twilio
vox campaign run --limit 25                # every lead that is due
vox call show <call-id>                    # transcript, tool calls, cost
vox call simulate <lead-id> --persona interested_01   # text rehearsal, persisted like a real call
```

The same operations are available over HTTP (`POST /calls/dial`, `POST /campaigns/run`,
`GET /calls/{id}`) — see `/docs`.

`campaign run` only dials leads that are due: not `do_not_call`, not `booked`/`rejected`/
`exhausted`, `next_attempt_at` in the past, and inside the calling window. It is
idempotent and safe to run from cron:

```cron
*/30 10-18 * * 1-6  cd /srv/vox && .venv/bin/vox campaign run --limit 20 >> /var/log/vox.log 2>&1
```

## 5. Cost tracking

Every call writes one `cost_logs` row: STT seconds, LLM input/output tokens, TTS
characters and telephony seconds, each priced with `app/costs/rates.py` and summed into
`cost_usd`, with the per-component breakdown kept as JSON so a run can be re-priced when
a provider changes its rate card.

```bash
vox costs report                 # last 7 days
vox costs report --days 30
curl localhost:8000/reports/costs
```

The report gives total spend, cost per call, cost per *booked meeting* (the number that
decides whether this is worth running), the component split, and outcome counts.
Reference figures from the eval baseline: **$0.1623 per simulated call**; a real call is
dominated by ElevenLabs characters and Twilio minutes, both of which drop sharply if you
switch `TTS_PRIMARY_PROVIDER=piper`.

## 6. Database and migrations

```bash
make migrate                       # alembic upgrade head
make migration m="add lead source" # autogenerate, then READ the diff
alembic downgrade -1
```

Migrations run against both SQLite and Postgres (`render_as_batch` is enabled for
SQLite's limited `ALTER TABLE`). `migrations/script.py.mako` imports `app.db.base`, so
the custom `GUID` type is emitted correctly by autogenerate — do not hand-edit generated
revisions to fix imports.

`vox db init` (create_all) is a **dev-only** shortcut. Production goes through Alembic;
CI proves `upgrade head` → `downgrade base` is clean on every push.

## 7. Deployment

The service is stateful for the duration of a call (an open websocket plus in-memory
conversation state), so it must run somewhere that supports long-lived connections and
does not scale to zero mid-call. Serverless-with-cold-starts is the wrong shape.

**Container**

```bash
make docker
docker run --rm -p 8000:8000 --env-file .env vox-orchestrator:local
```

One uvicorn worker per container by design — scale out with instances, not workers, so a
call's in-memory state always belongs to the process holding its websocket.

**Koyeb** (recommended free option): create a service from the Dockerfile, set the env
vars from `.env.example`, expose port 8000, health check `GET /health`, min instances 1.
Set `KOYEB_API_TOKEN` and `KOYEB_SERVICE` as repository secrets and
`ci/github-actions/deploy.yml` redeploys on every merge to `main` once it is moved into
`.github/workflows/` — see [`ci/github-actions/README.md`](../ci/github-actions/README.md).

**Cloud Run**: same image; deploy with `--min-instances 1 --timeout 900` (websockets need
the long request timeout, min-instances avoids cold starts on an inbound webhook). The
workflow does this via Workload Identity Federation — no service-account JSON key.

Both deploy jobs skip themselves when their secrets are absent, so a fork with no
credentials still gets a green pipeline.

**After any deploy**: update `PUBLIC_BASE_URL` to the new hostname, then
`curl $PUBLIC_BASE_URL/health` and confirm `database: ok` and the expected provider list.

## 8. Observability

* Logs: `structlog`, JSON when `LOG_JSON=true`, with `call_id` bound to every line in a
  call. `grep '"call_id":"<uuid>"'` gives the complete history of one call.
* Traces: set `TRACING_ENABLED=true` and `PHOENIX_COLLECTOR_ENDPOINT`; spans cover each
  turn, LLM call and tool invocation. `pip install -e ".[tracing]"` first.
* `GET /health` reports database, cache, configured providers, whether pipecat is
  installed, and the compliance flags.

## 9. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `403` on every Twilio webhook | `PUBLIC_BASE_URL` ≠ the URL Twilio called; or the auth token rotated |
| Call connects then goes silent | `pipecat` not installed, or the websocket URL is `ws://` instead of `wss://` |
| `CallRejected: outside the permitted calling window` | IST window in `.env`; the server clock is UTC, the window is evaluated in `CALLING_TIMEZONE` |
| `CallRejected: TRAI DLT registration is not marked complete` | `APP_ENV=production` with `COMPLIANCE_DLT_REGISTERED=false` — intentional |
| Every call `failed` immediately on a trial account | destination number not verified in the Twilio console |
| `The python-multipart library must be installed` | dependencies out of date; `pip install -r requirements.txt` |
| `MissingGreenlet` after touching a repository | a bulk `UPDATE` crept in — see the repository rules in [`architecture.md`](./architecture.md#repository-pattern) |
| Eval scores drop after a prompt edit | intended behaviour; read `eval/reports/*-vs-baseline.md` before overwriting the baseline |
