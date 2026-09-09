# Eval report — `baseline`

- Prompt variant: `v2-strict-tools`
- Agent LLM: `mock` · Caller: `scripted`
- Commit: `6ad9aa5`
- Run: `2026-09-08T20:06:58.872988+00:00` → `2026-09-08T20:06:59.149248+00:00`

## Headline metrics

| Metric | Value |
|---|---:|
| Personas run | 46 |
| Task completion rate | **100.0%** |
| Correct tool sequence rate | **100.0%** |
| Hallucinated tool-call rate | **0.0%** |
| Avg turns to resolution | **3.76** |
| Fully passing cases | 100.0% |
| Booking rate | 52.2% |
| Simulated cost (total / call) | $7.4645 / $0.1623 |

## By persona category

| Category | N | Task completion | Correct tools | Hallucination | Avg turns |
|---|---:|---:|---:|---:|---:|
| asr | 1 | 100% | 100% | 0% | 4 |
| code_switching | 3 | 100% | 100% | 0% | 4.33 |
| interested | 6 | 100% | 100% | 0% | 4.17 |
| need_to_think | 4 | 100% | 100% | 0% | 5 |
| no_budget | 4 | 100% | 100% | 0% | 5 |
| not_interested | 5 | 100% | 100% | 0% | 4 |
| price | 6 | 100% | 100% | 0% | 5.33 |
| rude | 4 | 100% | 100% | 0% | 1.75 |
| silence | 2 | 100% | 100% | 0% | 1 |
| timing | 5 | 100% | 100% | 0% | 5.2 |
| voicemail | 2 | 100% | 100% | 0% | 0 |
| wrong_person | 4 | 100% | 100% | 0% | 1 |

## Failures (0)

None — every persona reached its expected outcome with a valid tool sequence.

## Per-persona detail

| Persona | Outcome | Turns | Tools | Pass |
|---|---|---:|---|:--:|
| `budget_01` | not_interested | 5 | confirm_person → capture_discovery → classify_objection → classify_objection → mark_not_interested | ✅ |
| `budget_02` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `budget_03` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `budget_04` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `hinglish_01` | meeting_booked | 4 | confirm_person → capture_discovery → move_to_close → schedule_meeting | ✅ |
| `hinglish_02` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `hinglish_03` | not_interested | 4 | confirm_person → capture_discovery → classify_objection → mark_not_interested | ✅ |
| `interested_01` | meeting_booked | 4 | confirm_person → capture_discovery → move_to_close → schedule_meeting | ✅ |
| `interested_02` | meeting_booked | 4 | confirm_person → capture_discovery → move_to_close → schedule_meeting | ✅ |
| `interested_03` | meeting_booked | 4 | confirm_person → capture_discovery → move_to_close → schedule_meeting | ✅ |
| `interested_04` | meeting_booked | 4 | confirm_person → capture_discovery → move_to_close → schedule_meeting | ✅ |
| `interested_05` | meeting_booked | 4 | confirm_person → capture_discovery → move_to_close → schedule_meeting | ✅ |
| `interested_06` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `noisy_01` | meeting_booked | 4 | confirm_person → capture_discovery → move_to_close → schedule_meeting | ✅ |
| `notint_01` | not_interested | 4 | confirm_person → capture_discovery → classify_objection → mark_not_interested | ✅ |
| `notint_02` | not_interested | 4 | confirm_person → capture_discovery → classify_objection → mark_not_interested | ✅ |
| `notint_03` | not_interested | 4 | confirm_person → capture_discovery → classify_objection → mark_not_interested | ✅ |
| `notint_04` | not_interested | 4 | confirm_person → capture_discovery → classify_objection → mark_not_interested | ✅ |
| `notint_05` | not_interested | 4 | confirm_person → capture_discovery → classify_objection → mark_not_interested | ✅ |
| `price_01` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `price_02` | meeting_booked | 6 | confirm_person → capture_discovery → classify_objection → classify_objection → move_to_close → schedule_meeting | ✅ |
| `price_03` | not_interested | 5 | confirm_person → capture_discovery → classify_objection → classify_objection → mark_not_interested | ✅ |
| `price_04` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `price_05` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `price_06` | meeting_booked | 6 | confirm_person → capture_discovery → classify_objection → classify_objection → move_to_close → schedule_meeting | ✅ |
| `rude_01` | hung_up | 0 | — | ✅ |
| `rude_02` | hung_up | 2 | confirm_person → capture_discovery | ✅ |
| `rude_03` | hung_up | 1 | confirm_person | ✅ |
| `rude_04` | not_interested | 4 | confirm_person → capture_discovery → classify_objection → mark_not_interested | ✅ |
| `silence_01` | no_answer | 0 | — | ✅ |
| `silence_02` | no_answer | 2 | confirm_person → capture_discovery | ✅ |
| `think_01` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `think_02` | not_interested | 5 | confirm_person → capture_discovery → classify_objection → classify_objection → mark_not_interested | ✅ |
| `think_03` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `think_04` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `timing_01` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `timing_02` | not_interested | 5 | confirm_person → capture_discovery → classify_objection → classify_objection → mark_not_interested | ✅ |
| `timing_03` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `timing_04` | meeting_booked | 5 | confirm_person → capture_discovery → classify_objection → move_to_close → schedule_meeting | ✅ |
| `timing_05` | meeting_booked | 6 | confirm_person → capture_discovery → classify_objection → classify_objection → move_to_close → schedule_meeting | ✅ |
| `voicemail_01` | voicemail | 0 | log_voicemail | ✅ |
| `voicemail_02` | voicemail | 0 | log_voicemail | ✅ |
| `wrong_01` | wrong_person | 1 | confirm_person | ✅ |
| `wrong_02` | wrong_person | 1 | confirm_person | ✅ |
| `wrong_03` | unavailable | 1 | confirm_person | ✅ |
| `wrong_04` | wrong_person | 1 | confirm_person | ✅ |
