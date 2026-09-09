"""Deterministic, offline "LLM" used by the eval harness, the tests and demos.

It is a rule-based policy that reads the same system/state prompts a real model
sees and emits the same `LLMResponse` shape (speech + tool calls). That makes
the whole agent — engine, validation, guards, cost tracking, scoring — runnable
with zero API keys and zero network, which is what keeps CI honest.

It is also deliberately sensitive to the `TOOL DISCIPLINE:` marker in the system
prompt, so `PROMPT_VARIANTS` produce measurably different eval scores (Phase 6
regression demo). Failure injection (`fail_times`, `mode`) lets the tests drive
the router's fallback path and the engine's validation-retry path.
"""

from __future__ import annotations

import re
import time
from typing import Any

from app.enums import ConversationState, ObjectionType
from app.llm.base import LLMClient, LLMError, LLMResponse, Message, ToolCall, Usage
from app.pipeline.prompts import OBJECTION_REBUTTALS, SCRIPTED_LINES

_STATE_RE = re.compile(r"CURRENT STATE:\s*([a-z_]+)")

_YES = (
    "yes",
    "yeah",
    "yep",
    "speaking",
    "that's me",
    "thats me",
    "this is he",
    "this is she",
    "i am the owner",
    "i'm the owner",
    "owner here",
    "bol raha",
    "bolo",
)
_WRONG_PERSON = (
    "wrong number",
    "no one by that name",
    "not here",
    "isn't here",
    "isnt here",
    "he is out",
    "she is out",
    "not available",
    "unavailable",
    "call back later",
    "i'm just staff",
    "im just staff",
    "i just work here",
    "manager is not",
)
_INTEREST = (
    "how does that work",
    "how would that work",
    "tell me more",
    "interested",
    "sounds good",
    "sounds interesting",
    "go ahead",
    "sure",
    "okay let's",
    "ok let's",
    "let's do",
    "lets do",
    "why not",
    "send me",
    "book it",
    "what's the plan",
    "that works",
    "works for me",
    "sounds fine",
    "alright",
    "yes",
    "okay",
    " ok",
)
_HARD_NO = (
    # True opt-out signals only. A plain "not interested" is an *objection* that
    # must be classified first (strict rule 3), not a hard stop.
    "stop calling",
    "don't call",
    "dont call",
    "remove me",
    "do not call",
    "take me off",
    "hang up",
    "waste of time",
    "get lost",
    "don't call again",
    "dont call again",
)
_ACCEPT_REBUTTAL = (
    "makes sense",
    "fair enough",
    "alright",
    "all right",
    "ok fine",
    "okay fine",
    "fine",
    "that's fair",
    "thats fair",
    "i see",
    "understood",
    "hmm ok",
    "okay",
    "ok",
)
_OBJECTION_KEYWORDS: dict[ObjectionType, tuple[str, ...]] = {
    ObjectionType.PRICE: (
        "expensive",
        "costly",
        "how much",
        "what's the cost",
        "price",
        "charges",
        "rate",
        "kitna",
    ),
    ObjectionType.NO_BUDGET: (
        "no budget",
        "budget",
        "can't afford",
        "cant afford",
        "no money",
        "tight",
        "spend",
    ),
    ObjectionType.TIMING: (
        "busy",
        "not right now",
        "later",
        "next month",
        "next quarter",
        "call me next",
        "season",
        "after",
        "some other time",
    ),
    ObjectionType.NEED_TO_THINK: (
        "think about it",
        "let me think",
        "discuss",
        "partner",
        "get back to you",
        "consider",
        "check with",
    ),
    ObjectionType.NOT_INTERESTED: (
        "not interested",
        "don't need",
        "dont need",
        "already have",
        "we handle",
        "in-house",
        "no thanks",
    ),
}
_TIME_RE = re.compile(
    r"\b(?:(?:mon|tues|tue|wednes|wed|thurs|thu|fri|satur|sat|sun)[a-z]*|tomorrow|today)\b",
    re.IGNORECASE,
)
_CLOCK_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|o'clock|oclock|baje)?\b", re.IGNORECASE)


def _contains(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _classify_objection(text: str) -> ObjectionType | None:
    for objection, needles in _OBJECTION_KEYWORDS.items():
        if _contains(text, needles):
            return objection
    return None


def _parse_slot(text: str) -> tuple[str, str] | None:
    """Extract a (date, time) pair from a lead utterance, if one is present."""
    from datetime import date, timedelta

    day_match = _TIME_RE.search(text)
    if not day_match:
        return None
    token = day_match.group(0).lower()
    today = date.today()
    weekdays = {
        "mon": 0,
        "tue": 1,
        "tues": 1,
        "wed": 2,
        "wednes": 2,
        "thu": 3,
        "thur": 3,
        "thurs": 3,
        "fri": 4,
        "sat": 5,
        "satur": 5,
        "sun": 6,
    }
    if token == "today":
        target = today
    elif token == "tomorrow":
        target = today + timedelta(days=1)
    else:
        prefix = token[:5]
        index = next((v for k, v in weekdays.items() if prefix.startswith(k)), None)
        if index is None:
            return None
        delta = (index - today.weekday()) % 7 or 7
        target = today + timedelta(days=delta)

    hour, minute = 11, 0
    for raw_hour, raw_minute, meridiem in _CLOCK_RE.findall(text):
        value = int(raw_hour)
        if value == 0 or value > 24:
            continue
        minute = int(raw_minute or 0)
        marker = (meridiem or "").lower()
        if marker == "pm" and value < 12:
            value += 12
        elif not marker and value <= 7:
            value += 12  # "at 4" on a business call means 16:00
        hour = min(value, 23)
        break
    return target.isoformat(), f"{hour:02d}:{minute:02d}"


class MockLLMClient(LLMClient):
    """Rule-based stand-in for Groq/Gemini.

    Args:
        mode: ``"competent"`` (default) plays the agent correctly;
            ``"hallucinating"`` emits an out-of-state tool call on the first
            turn so the validation-retry path can be exercised;
            ``"malformed"`` emits invalid arguments.
        fail_times: raise `LLMError` for the first N calls (router fallback tests).
        agency_name/agent_name: used to render the scripted speech.
    """

    provider = "mock"

    def __init__(
        self,
        *,
        mode: str = "competent",
        fail_times: int = 0,
        agent_name: str = "Riya",
        agency_name: str = "Northstar Digital",
        model: str = "mock-policy-v2",
    ) -> None:
        self.mode = mode
        self.fail_times = fail_times
        self.agent_name = agent_name
        self.agency_name = agency_name
        self.model = model
        self.calls = 0
        self._misfired = False

    def is_configured(self) -> bool:
        return True

    # --- helpers ---------------------------------------------------------
    @staticmethod
    def _state_from_messages(messages: list[Message]) -> ConversationState:
        for message in reversed(messages):
            match = _STATE_RE.search(message.content or "")
            if match:
                try:
                    return ConversationState(match.group(1))
                except ValueError:  # pragma: no cover - defensive
                    break
        return ConversationState.GREETING

    @staticmethod
    def _strict(messages: list[Message]) -> bool:
        return any("TOOL DISCIPLINE: STRICT" in (m.content or "") for m in messages)

    @staticmethod
    def _last_user(messages: list[Message]) -> str:
        for message in reversed(messages):
            if message.role != "user":
                continue
            content = (message.content or "").strip()
            # Skip the validation-feedback message the engine injects on a retry:
            # the lead's actual last utterance is what the policy reacts to.
            if "was rejected" in content:
                continue
            return content.lower()
        return ""

    @staticmethod
    def _objection_count(messages: list[Message]) -> int:
        return sum(
            1 for m in messages if m.role == "tool" and "classify_objection" in (m.content or "")
        )

    # --- policy ----------------------------------------------------------
    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        self.calls += 1
        if self.fail_times >= self.calls:
            raise LLMError(f"mock injected failure #{self.calls}", provider=self.provider)

        state = self._state_from_messages(messages)
        user = self._last_user(messages)
        strict = self._strict(messages)
        available = {(tool.get("function", tool) or {}).get("name") for tool in (tools or [])}
        retry_hint = any("was rejected" in (m.content or "") for m in messages[-3:])

        if self.mode == "hallucinating" and not self._misfired and not retry_hint:
            self._misfired = True
            return self._respond(
                "Great, let me get that booked for you.",
                ToolCall(
                    "schedule_meeting",
                    {"date": "2099-01-01", "time": "11:00", "contact_confirmation": "n/a"},
                ),
                started,
            )
        if self.mode == "malformed" and not self._misfired and not retry_hint:
            self._misfired = True
            return self._respond(
                "One second.",
                ToolCall("confirm_person", {"is_correct_person": "definitely"}),
                started,
            )

        text, call = self._decide(state, user, strict, available, messages)
        return self._respond(text, call, started)

    def _decide(
        self,
        state: ConversationState,
        user: str,
        strict: bool,
        available: set[str | None],
        messages: list[Message],
    ) -> tuple[str, ToolCall | None]:
        # A hard stop is honoured from any state.
        if _contains(user, _HARD_NO) and "mark_not_interested" in available:
            return (
                SCRIPTED_LINES["not_interested"],
                ToolCall("mark_not_interested", {"reason": f"lead said: {user[:120]}"}),
            )

        if state is ConversationState.GREETING:
            return SCRIPTED_LINES["greeting_no_area"].format(
                agent_name=self.agent_name,
                agency_name=self.agency_name,
                business_name="your business",
            ), None

        if state is ConversationState.CONFIRM_PERSON:
            if _contains(user, _WRONG_PERSON):
                return (
                    SCRIPTED_LINES["wrong_person"],
                    ToolCall(
                        "confirm_person",
                        {
                            "is_correct_person": False,
                            "reason": user[:200] or "not the decision maker",
                        },
                    ),
                )
            if _contains(user, _YES) or len(user.split()) <= 6:
                return (
                    SCRIPTED_LINES["discovery_question"],
                    ToolCall("confirm_person", {"is_correct_person": True}),
                )
            return SCRIPTED_LINES["confirm_person_prompt"], None

        if state is ConversationState.DISCOVERY:
            return (
                SCRIPTED_LINES["pitch"],
                ToolCall(
                    "capture_discovery",
                    {"current_marketing": (user or "not stated")[:300], "decision_authority": True},
                ),
            )

        if state is ConversationState.PITCH:
            objection = _classify_objection(user)
            interested = _contains(user, _INTEREST) or _parse_slot(user) is not None
            if objection and strict:
                # Strict rule 3: classify before doing anything else.
                return (
                    OBJECTION_REBUTTALS[objection],
                    ToolCall(
                        "classify_objection", {"type": objection.value, "verbatim": user[:200]}
                    ),
                )
            if objection and not strict:
                # The loose variant "keeps the momentum going" and pushes for the
                # meeting without ever classifying the objection — exactly the
                # failure mode the strict rules were written to prevent.
                return (
                    SCRIPTED_LINES["close_ask"],
                    ToolCall("move_to_close", {"interest_signal": (user or "kept talking")[:200]}),
                )
            if interested:
                return (
                    SCRIPTED_LINES["close_ask"],
                    ToolCall(
                        "move_to_close", {"interest_signal": (user or "positive reaction")[:200]}
                    ),
                )
            return SCRIPTED_LINES["pitch"], None

        if state is ConversationState.OBJECTION_HANDLING:
            objection = _classify_objection(user)
            cycles = self._objection_count(messages)
            if _contains(user, _INTEREST):
                return (
                    SCRIPTED_LINES["close_ask"],
                    ToolCall("move_to_close", {"interest_signal": user[:200]}),
                )
            if _contains(user, _ACCEPT_REBUTTAL):
                # resolve_objection returns the conversation to the pitch, so
                # the spoken line has to be the re-pitch, not the close.
                return (
                    SCRIPTED_LINES["pitch"],
                    ToolCall(
                        "resolve_objection",
                        {
                            "objection_type": (objection or ObjectionType.NEED_TO_THINK).value,
                            "resolved": True,
                            "rebuttal_summary": "lead accepted the rebuttal",
                        },
                    ),
                )
            if objection and cycles < 3:
                return (
                    OBJECTION_REBUTTALS[objection],
                    ToolCall(
                        "classify_objection", {"type": objection.value, "verbatim": user[:200]}
                    ),
                )
            return (
                SCRIPTED_LINES["not_interested"],
                ToolCall(
                    "mark_not_interested",
                    {"reason": (user or "objection could not be resolved")[:200]},
                ),
            )

        if state is ConversationState.CLOSE:
            objection = _classify_objection(user)
            slot = _parse_slot(user)
            if slot:
                date_str, time_str = slot
                return (
                    SCRIPTED_LINES["booking_confirm"].format(when=f"{date_str} at {time_str}"),
                    ToolCall(
                        "schedule_meeting",
                        {
                            "date": date_str,
                            "time": time_str,
                            "contact_confirmation": "confirmed on this number",
                        },
                    ),
                )
            if objection and strict:
                return (
                    OBJECTION_REBUTTALS[objection],
                    ToolCall(
                        "classify_objection", {"type": objection.value, "verbatim": user[:200]}
                    ),
                )
            if _contains(user, _INTEREST) or _contains(user, _ACCEPT_REBUTTAL):
                return SCRIPTED_LINES["close_ask"], None
            return SCRIPTED_LINES["close_ask"], None

        if state is ConversationState.BOOK_MEETING:
            return (
                SCRIPTED_LINES["goodbye"],
                ToolCall(
                    "end_call",
                    {"outcome": "meeting_booked", "reason": "meeting scheduled with the owner"},
                ),
            )

        return SCRIPTED_LINES["goodbye"], None

    def _respond(self, text: str, call: ToolCall | None, started: float) -> LLMResponse:
        return LLMResponse(
            text=text,
            tool_calls=[call] if call else [],
            usage=Usage(
                input_tokens=max(len(text) // 4, 1) + 220, output_tokens=max(len(text) // 4, 1)
            ),
            provider=self.provider,
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


__all__ = ["MockLLMClient"]
