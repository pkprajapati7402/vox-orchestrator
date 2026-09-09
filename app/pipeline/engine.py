"""The conversation engine.

Transport-agnostic implementation of the state machine in Project-Details.md §4.
The same object powers three very different callers:

  * the live phone call  (Pipecat feeds it STT text, takes back speech for TTS)
  * the eval harness     (a persona LLM feeds it text, no audio at all)
  * the tests            (hand-written utterances, mock LLM)

Turn contract
-------------
`start()` produces the greeting and moves to ConfirmPerson.
`handle_user(text, confidence)` produces exactly one `AgentTurn`.
`handle_silence()` / `handle_voicemail()` / `abort()` cover the non-speech paths.
Every state change is the result of a *validated* tool call, or of a guard —
never of free-form model output.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.config import Settings, get_settings
from app.costs.tracker import CostAccumulator
from app.enums import CallOutcome, ConversationState, TurnRole
from app.llm.base import LLMClient, LLMError, LLMResponse, Message
from app.logging_config import get_logger
from app.pipeline.guards import (
    ASRConfidenceGate,
    ObjectionLoopGuard,
    SilenceGuard,
    ToolRetryBudget,
    TurnBudget,
)
from app.pipeline.prompts import PROMPT_VERSION, build_system_prompt, scripted, state_prompt
from app.pipeline.states import can_transition, default_outcome
from app.tools.definitions import tool_schemas_for_state
from app.tools.registry import ToolInvocation, ToolValidationError, execute_tool

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LeadContext:
    """Everything the agent knows about the business before dialling."""

    business_name: str
    phone: str = ""
    category: str | None = None
    address: str | None = None
    contact_name: str | None = None
    research_notes: str | None = None
    lead_id: uuid.UUID | None = None

    @classmethod
    def from_model(cls, lead: Any) -> LeadContext:
        return cls(
            business_name=lead.business_name,
            phone=lead.phone,
            category=lead.category,
            address=lead.address,
            contact_name=lead.contact_name,
            research_notes=lead.research_notes,
            lead_id=lead.id,
        )


@dataclass
class TurnRecord:
    """One transcript row, emitted to the sink as it happens."""

    role: TurnRole
    content: str
    state: ConversationState
    tool_called: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_valid: bool | None = None
    asr_confidence: float | None = None
    latency_ms: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now())


@dataclass
class AgentTurn:
    """What the agent produced this turn, plus why."""

    text: str
    state_before: ConversationState
    state_after: ConversationState
    invocation: ToolInvocation | None = None
    tool_error: str | None = None
    hallucinated: bool = False
    used_fallback_line: bool = False
    ended: bool = False
    outcome: CallOutcome | None = None
    latency_ms: int = 0

    @property
    def tool_name(self) -> str | None:
        return self.invocation.name if self.invocation else None

    @property
    def transitioned(self) -> bool:
        return self.state_before is not self.state_after


class TurnSink(Protocol):
    """Anything that wants to observe turns (DB persistence, tracing, tests)."""

    async def on_turn(self, record: TurnRecord) -> None: ...


SpeechHook = Callable[[str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class ConversationEngine:
    """Drives one call from Greeting to Wrapup."""

    def __init__(
        self,
        lead: LeadContext,
        llm: LLMClient,
        *,
        settings: Settings | None = None,
        prompt_variant: str = PROMPT_VERSION,
        cost: CostAccumulator | None = None,
        sink: TurnSink | None = None,
        call_id: uuid.UUID | None = None,
    ) -> None:
        self.lead = lead
        self.llm = llm
        self.settings = settings or get_settings()
        self.prompt_variant = prompt_variant
        self.cost = cost or CostAccumulator()
        self.sink = sink
        self.call_id = call_id or uuid.uuid4()

        self.state: ConversationState = ConversationState.GREETING
        self.outcome: CallOutcome | None = None
        self.outcome_reason: str | None = None
        self.ended = False
        self.meeting_at: datetime | None = None
        self.meeting_contact: str | None = None

        self.history: list[Message] = []
        self.transcript: list[TurnRecord] = []
        self.tool_sequence: list[str] = []
        self.rejected_tool_calls: list[dict[str, Any]] = []
        self.hallucinated_tool_calls = 0
        self.invalid_argument_calls = 0
        self.fallback_lines_used = 0
        self.llm_errors = 0

        self.objection_guard = ObjectionLoopGuard(max_cycles=self.settings.max_objection_cycles)
        self.silence_guard = SilenceGuard(max_prompts=1)
        self.asr_gate = ASRConfidenceGate(
            min_confidence=self.settings.stt_min_confidence,
            max_repeats=self.settings.max_asr_repeat_requests,
        )
        self.turn_budget = TurnBudget(max_turns=self.settings.max_conversation_turns)

        self._system_prompt = build_system_prompt(
            agent_name=self.settings.agent_name,
            agency_name=self.settings.agency_name,
            business_name=lead.business_name,
            category=lead.category,
            address=lead.address,
            contact_name=lead.contact_name,
            research_notes=lead.research_notes,
            variant=prompt_variant,
        )
        self.started_at = time.time()

    # --- public API -------------------------------------------------------
    async def start(self) -> AgentTurn:
        """Speak the opening line and move Greeting -> ConfirmPerson."""
        line = self._greeting_line()
        turn = AgentTurn(
            text=line,
            state_before=ConversationState.GREETING,
            state_after=ConversationState.CONFIRM_PERSON,
        )
        self.state = ConversationState.CONFIRM_PERSON
        self.cost.add_tts(len(line), self.settings.elevenlabs_model)
        await self._record(TurnRole.AGENT, line, ConversationState.GREETING)
        return turn

    async def handle_user(self, text: str, confidence: float | None = None) -> AgentTurn:
        """Process one lead utterance and return the agent's reply."""
        if self.ended:
            return AgentTurn(text="", state_before=self.state, state_after=self.state, ended=True)

        started = time.perf_counter()
        self.silence_guard.reset()
        text = (text or "").strip()

        # 1. ASR confidence gate -------------------------------------------
        if self.asr_gate.should_ask_repeat(confidence):
            self.asr_gate.register_repeat()
            line = scripted("asr_repeat")
            await self._record(TurnRole.LEAD, text, self.state, asr_confidence=confidence)
            await self._speak(line)
            return AgentTurn(
                text=line,
                state_before=self.state,
                state_after=self.state,
                used_fallback_line=True,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        await self._record(TurnRole.LEAD, text, self.state, asr_confidence=confidence)
        self.history.append(Message(role="user", content=text or "(no response)"))
        self.turn_budget.register()

        # 2. hard turn cap --------------------------------------------------
        if self.turn_budget.exhausted:
            return await self._force_end(
                CallOutcome.FAILED,
                "conversation exceeded the maximum turn budget",
                scripted("max_turns"),
            )

        # 3. ask the model --------------------------------------------------
        state_before = self.state
        retry_budget = ToolRetryBudget(max_retries=self.settings.max_tool_validation_retries)
        invocation: ToolInvocation | None = None
        tool_error: str | None = None
        hallucinated = False
        response: LLMResponse | None = None

        while True:
            try:
                response = await self._call_llm()
            except LLMError as exc:
                self.llm_errors += 1
                log.error("engine.llm_unavailable", error=str(exc)[:300], call_id=str(self.call_id))
                line = scripted("safe_fallback")
                self.fallback_lines_used += 1
                await self._speak(line)
                return AgentTurn(
                    text=line,
                    state_before=state_before,
                    state_after=self.state,
                    tool_error=str(exc),
                    used_fallback_line=True,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )

            call = response.tool_call
            if call is None:
                break

            try:
                invocation = execute_tool(call.name, call.arguments, self.state)
                if not can_transition(invocation.from_state, invocation.to_state):
                    raise ToolValidationError(
                        f"transition {invocation.from_state.value} -> {invocation.to_state.value} "
                        "is not permitted by the state machine",
                        kind="illegal_state",
                        tool_name=call.name,
                    )
                break
            except ToolValidationError as exc:
                invocation = None
                tool_error = str(exc)
                if exc.is_hallucination:
                    hallucinated = True
                    self.hallucinated_tool_calls += 1
                else:
                    self.invalid_argument_calls += 1
                self.rejected_tool_calls.append(
                    {
                        "tool": call.name,
                        "args": call.arguments,
                        "state": self.state.value,
                        "kind": exc.kind,
                        "error": str(exc),
                    }
                )
                log.warning(
                    "engine.tool_rejected",
                    tool=call.name,
                    kind=exc.kind,
                    state=self.state.value,
                    call_id=str(self.call_id),
                )
                await self._record(
                    TurnRole.SYSTEM,
                    f"tool call rejected: {exc}",
                    self.state,
                    tool_called=call.name,
                    tool_args=call.arguments,
                    tool_valid=False,
                )
                if retry_budget.can_retry():
                    retry_budget.consume()
                    self.history.append(
                        Message(role="assistant", content=response.text or "(tool call)")
                    )
                    self.history.append(Message(role="user", content=exc.feedback(self.state)))
                    continue
                break

        assert response is not None  # loop always assigns before breaking

        # 4. apply the transition -------------------------------------------
        speech = (response.text or "").strip()
        used_fallback = False
        if invocation is None and tool_error is not None:
            speech = scripted("safe_fallback")
            self.fallback_lines_used += 1
            used_fallback = True
        elif not speech:
            speech = self._default_line_for(self.state)
            used_fallback = True

        if invocation is not None:
            self.tool_sequence.append(invocation.name)
            await self._apply_invocation(invocation, speech)
        else:
            await self._speak(speech)

        latency_ms = int((time.perf_counter() - started) * 1000)
        self.history.append(Message(role="assistant", content=speech))
        if invocation is not None:
            self.history.append(
                Message(
                    role="tool",
                    name=invocation.name,
                    content=f"{invocation.name} accepted -> state={self.state.value}",
                )
            )

        return AgentTurn(
            text=speech,
            state_before=state_before,
            state_after=self.state,
            invocation=invocation,
            tool_error=tool_error,
            hallucinated=hallucinated,
            used_fallback_line=used_fallback,
            ended=self.ended,
            outcome=self.outcome,
            latency_ms=latency_ms,
        )

    async def handle_silence(self) -> AgentTurn:
        """No speech within the silence timeout: prompt once, then hang up."""
        if self.ended:
            return AgentTurn(text="", state_before=self.state, state_after=self.state, ended=True)
        if self.silence_guard.next_action() == "prompt":
            line = scripted("silence_prompt")
            await self._speak(line)
            return AgentTurn(text=line, state_before=self.state, state_after=self.state)
        return await self._force_end(
            CallOutcome.NO_ANSWER, "silence timeout with no response", scripted("silence_goodbye")
        )

    async def handle_voicemail(self, detail: str | None = None) -> AgentTurn:
        """Twilio AMD says a machine answered: log it and hang up, no pitch."""
        state_before = self.state
        try:
            invocation = execute_tool("log_voicemail", {"detail": detail}, self.state)
        except ToolValidationError:  # pragma: no cover - log_voicemail is always allowed
            return await self._force_end(CallOutcome.VOICEMAIL, "voicemail detected", "")
        self.tool_sequence.append(invocation.name)
        await self._apply_invocation(invocation, "", speak=False)
        return AgentTurn(
            text="",
            state_before=state_before,
            state_after=self.state,
            invocation=invocation,
            ended=True,
            outcome=self.outcome,
        )

    async def abort(self, outcome: CallOutcome, reason: str) -> AgentTurn:
        """Terminate without speaking (lead hung up, transport dropped, crash)."""
        return await self._force_end(outcome, reason, "")

    # --- internals --------------------------------------------------------
    async def _call_llm(self) -> LLMResponse:
        messages = self._build_messages()
        response = await self.llm.complete(messages, tool_schemas_for_state(self.state))
        self.cost.add_llm(response.usage.input_tokens, response.usage.output_tokens, response.model)
        return response

    def _build_messages(self) -> list[Message]:
        return [
            Message(role="system", content=self._system_prompt),
            Message(role="system", content=state_prompt(self.state)),
            *self.history,
        ]

    async def _apply_invocation(
        self, invocation: ToolInvocation, speech: str, *, speak: bool = True
    ) -> None:
        target = invocation.to_state

        # Objection loop guard (§7: "more than 3 cycles -> force Close or EndCall").
        if target is ConversationState.OBJECTION_HANDLING:
            self.objection_guard.register(invocation.metadata.get("objection_type"))
            forced = self.objection_guard.forced_state()
            if forced is not None:
                log.info(
                    "engine.objection_loop_guard",
                    cycles=self.objection_guard.cycles,
                    forced_to=forced.value,
                    call_id=str(self.call_id),
                )
                target = forced

        if invocation.metadata.get("meeting_at"):
            self.meeting_at = invocation.metadata["meeting_at"]
            self.meeting_contact = invocation.metadata.get("meeting_contact")

        await self._record(
            TurnRole.AGENT,
            speech,
            self.state,
            tool_called=invocation.name,
            tool_args=invocation.args_dict,
            tool_valid=True,
        )
        if speak and speech:
            self.cost.add_tts(len(speech), self.settings.elevenlabs_model)

        self.state = target

        if target is ConversationState.BOOK_MEETING:
            # BookMeeting -> Wrapup: the confirmation line above is the last thing said.
            self.outcome = CallOutcome.MEETING_BOOKED
            self.outcome_reason = "meeting scheduled"
            self._finish()
            return

        if target is ConversationState.END_CALL or invocation.terminal:
            outcome = invocation.outcome or default_outcome(invocation.from_state)
            reason = self._reason_from(invocation)
            self.outcome = self.outcome or outcome
            self.outcome_reason = self.outcome_reason or reason
            self._finish()

    @staticmethod
    def _reason_from(invocation: ToolInvocation) -> str:
        args = invocation.args_dict
        for key in ("reason", "detail", "rebuttal_summary", "interest_signal"):
            value = args.get(key)
            if value:
                return str(value)
        return f"{invocation.name} called"

    async def _force_end(self, outcome: CallOutcome, reason: str, line: str) -> AgentTurn:
        state_before = self.state
        if line:
            await self._speak(line)
        self.outcome = outcome
        self.outcome_reason = reason
        self.state = ConversationState.END_CALL
        self._finish()
        log.info(
            "engine.forced_end", outcome=outcome.value, reason=reason, call_id=str(self.call_id)
        )
        return AgentTurn(
            text=line,
            state_before=state_before,
            state_after=self.state,
            ended=True,
            outcome=outcome,
        )

    def _finish(self) -> None:
        self.state = ConversationState.WRAPUP
        self.ended = True
        self.cost.add_telephony(time.time() - self.started_at)

    async def _speak(self, line: str) -> None:
        if not line:
            return
        self.cost.add_tts(len(line), self.settings.elevenlabs_model)
        await self._record(TurnRole.AGENT, line, self.state)

    async def _record(
        self,
        role: TurnRole,
        content: str,
        state: ConversationState,
        **kwargs: Any,
    ) -> None:
        record = TurnRecord(role=role, content=content, state=state, **kwargs)
        self.transcript.append(record)
        if self.sink is not None:
            await self.sink.on_turn(record)

    def _greeting_line(self) -> str:
        area = (self.lead.address or "").split(",")[0].strip()
        key = "greeting" if area else "greeting_no_area"
        return scripted(
            key,
            agent_name=self.settings.agent_name,
            agency_name=self.settings.agency_name,
            business_name=self.lead.business_name,
            area=area,
        )

    def _default_line_for(self, state: ConversationState) -> str:
        mapping = {
            ConversationState.CONFIRM_PERSON: "confirm_person_prompt",
            ConversationState.DISCOVERY: "discovery_question",
            ConversationState.PITCH: "pitch",
            ConversationState.OBJECTION_HANDLING: "safe_fallback",
            ConversationState.CLOSE: "close_ask",
            ConversationState.BOOK_MEETING: "goodbye",
            ConversationState.END_CALL: "goodbye",
            ConversationState.WRAPUP: "goodbye",
        }
        return scripted(mapping.get(state, "safe_fallback"))

    # --- reporting --------------------------------------------------------
    @property
    def turns_taken(self) -> int:
        return self.turn_budget.turns

    def metrics(self) -> dict[str, Any]:
        return {
            "call_id": str(self.call_id),
            "final_state": self.state.value,
            "outcome": self.outcome.value if self.outcome else None,
            "outcome_reason": self.outcome_reason,
            "turns": self.turn_budget.turns,
            "tool_sequence": list(self.tool_sequence),
            "rejected_tool_calls": list(self.rejected_tool_calls),
            "hallucinated_tool_calls": self.hallucinated_tool_calls,
            "invalid_argument_calls": self.invalid_argument_calls,
            "fallback_lines_used": self.fallback_lines_used,
            "objection_cycles": self.objection_guard.cycles,
            "objection_types": list(self.objection_guard.types_seen),
            "asr_repeats": self.asr_gate.repeats_used,
            "llm_errors": self.llm_errors,
            "prompt_variant": self.prompt_variant,
            "cost": self.cost.snapshot().as_dict(),
            "meeting_at": self.meeting_at.isoformat() if self.meeting_at else None,
        }

    def transcript_text(self) -> str:
        lines = []
        for record in self.transcript:
            prefix = {TurnRole.AGENT: "AGENT", TurnRole.LEAD: "LEAD", TurnRole.SYSTEM: "SYS"}[
                record.role
            ]
            suffix = f"   [{record.tool_called}]" if record.tool_called else ""
            lines.append(f"{prefix}: {record.content}{suffix}")
        return "\n".join(lines)


__all__ = ["AgentTurn", "ConversationEngine", "LeadContext", "TurnRecord", "TurnSink"]
