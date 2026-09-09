# Compliance notes — outbound calling in India

**This project is not registered for commercial outbound calling.** Development and
testing are done exclusively against numbers verified in the Twilio console. What follows
is the checklist that must be completed *before* pointing it at a real lead list, and how
each item is enforced in code rather than in a wiki page nobody reads.

## What the law requires

Commercial voice calls to Indian subscribers fall under TRAI's TCCCPR framework:

1. **DLT registration.** The sender must be registered on a Distributed Ledger
   Technology platform operated by an access provider (Jio, Airtel, VI, BSNL),
   registering the entity, the header/CLI and the call templates/consent artefacts.
2. **National DNC / DND scrubbing.** Numbers on the National Customer Preference Register
   must not receive unsolicited commercial communication. The list has to be scrubbed
   before dialing, not after complaints arrive.
3. **Consent and opt-out.** Explicit consent must be recorded where relied upon, and any
   opt-out on the call must be honoured immediately and permanently.
4. **Calling hours.** Commercial calls are restricted to daytime hours; 09:00–19:00 IST
   is the conservative window used here.
5. **Identification.** The caller must identify itself and the entity it represents at
   the start of the call.

Anything below is engineering, not legal advice — get the registration reviewed by
someone qualified before dialing strangers.

## How each item is enforced

| Requirement | Enforcement | Where |
|---|---|---|
| DLT registration completed | With `APP_ENV=production` and `COMPLIANCE_DLT_REGISTERED=false`, **every dial is refused** | `app/telephony/compliance.py` |
| DNC scrubbing | `leads.do_not_call` blocks dialing (`COMPLIANCE_DNC_CHECK_ENABLED`); `POST /leads/{id}/do-not-call` flags a lead and returns `409` on any later dial attempt | `compliance.check_compliance`, `app/api/routes_leads.py` |
| Opt-out honoured | `mark_not_interested` sets the lead to `rejected` and clears the retry timer; the lead never re-enters `campaign run` | `CallService._apply_outcome_to_lead` |
| Calling hours | Dialing outside `CALLING_WINDOW_START_HOUR`..`CALLING_WINDOW_END_HOUR` in `CALLING_TIMEZONE` (default 09:00–19:00 Asia/Kolkata) is refused, evaluated in local time regardless of server timezone | `compliance.in_calling_window` |
| Identification | The greeting prompt names the agent and the agency (`AGENT_NAME`, `AGENCY_NAME`) in the first sentence, before anything is asked | `app/pipeline/prompts.py` |
| Auditability | Every turn, tool call and outcome is persisted in `transcript_turns` / `calls`, so any complaint can be reconstructed exactly | `app/db/models.py` |
| Attempt limits | `CALL_MAX_ATTEMPTS` (3) and the backoff ladder cap how often one number can be dialled | `app/services/call_service.py` |

The gate is a single function returning one allow/deny decision with human-readable
reasons, called before Twilio is ever contacted:

```python
decision = check_compliance(do_not_call=lead.do_not_call, phone=lead.phone)
if not decision.allowed:
    raise CallRejected(decision.reason)
```

`GET /health` surfaces `compliance.dlt_registered`, `compliance.dnc_check_enabled` and
`compliance.inside_calling_window`, so the state of the gate is visible without reading
the config.

## What is deliberately *not* automated

* **The DNC scrub itself.** There is no free, reliable programmatic feed of the National
  Customer Preference Register. The system models the outcome (`leads.do_not_call`) and
  expects the operator to import a scrub result — pretending to scrub would be worse than
  not scrubbing.
* **DLT template registration.** Handled in the access provider's portal;
  `COMPLIANCE_DLT_REGISTERED` is an attestation flag, not a check.

## AI disclosure

The agent identifies itself by name and agency in its first sentence, and **never denies
being an AI**: the system prompt forbids claiming to be human, and a scripted
`ai_disclosure` line ("Yes — I am an AI assistant calling on behalf of {agency}…") is used
the moment the question is asked. It does not, by default, volunteer the disclosure
unprompted. Regulation on synthetic-voice disclosure is moving quickly and several
jurisdictions already require proactive disclosure; moving that line into the greeting in
`app/pipeline/prompts.py` is a one-line change and is the right default if you deploy
this for real.

## Before the first real call

- [ ] DLT registration complete; `COMPLIANCE_DLT_REGISTERED=true`
- [ ] Lead list scrubbed against the National DNC; `do_not_call` set on every hit
- [ ] Consent basis documented for the remaining leads
- [ ] Calling window and timezone confirmed for the target region
- [ ] Greeting reviewed for identification (and, if required, AI disclosure)
- [ ] Twilio number provisioned with the registered CLI
- [ ] A human owner named for every booked meeting and every complaint
