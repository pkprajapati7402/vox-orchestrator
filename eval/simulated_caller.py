"""The simulated caller: a business owner played against the agent, text only.

Two implementations share one interface:

  * `ScriptedCaller` — deterministic, offline, driven by `Persona.behaviour`.
    This is the default because eval results have to be reproducible: if the
    score moves, it moved because the *agent* changed.
  * `LLMCaller` — a second LLM plays the persona for a fuzzier, more realistic
    run (`--caller llm`). Falls back to the scripted line if the model errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.enums import ConversationState, ObjectionType
from app.llm.base import LLMClient, LLMError, Message
from eval.personas import Persona

ActionKind = Literal["speak", "silence", "hangup"]


@dataclass(frozen=True)
class CallerAction:
    kind: ActionKind
    text: str = ""
    confidence: float | None = None


OBJECTION_LINES: dict[ObjectionType, tuple[str, ...]] = {
    ObjectionType.PRICE: (
        "Honestly that sounds expensive for us.",
        "What's the price though? Everyone quotes a high rate.",
    ),
    ObjectionType.NO_BUDGET: (
        "We have no budget for marketing right now.",
        "Budget is very tight this year, we can't spend on ads.",
    ),
    ObjectionType.TIMING: (
        "Not right now, we're very busy this season.",
        "Can you call me next quarter? Later would be better.",
    ),
    ObjectionType.NEED_TO_THINK: (
        "Let me think about it and discuss with my partner.",
        "I'll have to check with my partner and get back to you.",
    ),
    ObjectionType.NOT_INTERESTED: (
        "We already have someone handling this, not interested.",
        "We don't need it, we handle marketing in-house.",
    ),
}

RUDE_PREFIX = "Look, "
HINGLISH_SUFFIX = " Aap samajh rahe ho na?"


class ScriptedCaller:
    """Deterministic persona playback."""

    def __init__(self, persona: Persona) -> None:
        self.persona = persona
        self.behaviour = persona.behaviour
        self.objections_raised = 0
        self.rebuttals_heard = 0
        self.conceded = False
        self.turn = 0
        self.repeated_last = False
        self._last_line = ""

    # --- helpers ---------------------------------------------------------
    def _decorate(self, text: str) -> str:
        if self.behaviour.rude and not text.startswith(RUDE_PREFIX):
            text = RUDE_PREFIX + text[0].lower() + text[1:]
        if self.behaviour.code_switching:
            text = text + HINGLISH_SUFFIX
        return text

    def _objection_line(self) -> str | None:
        if self.objections_raised >= len(self.behaviour.objections):
            return None
        objection = self.behaviour.objections[self.objections_raised]
        variants = OBJECTION_LINES[objection]
        line = variants[min(self.objections_raised, len(variants) - 1)]
        self.objections_raised += 1
        return line

    @property
    def _should_concede(self) -> bool:
        return self.rebuttals_heard >= self.behaviour.concede_after

    # --- main API ---------------------------------------------------------
    async def reply(self, agent_text: str, state: ConversationState) -> CallerAction:
        self.turn += 1
        behaviour = self.behaviour

        if behaviour.hang_up_after_turn is not None and self.turn >= behaviour.hang_up_after_turn:
            return CallerAction("hangup")
        if self.turn in behaviour.silent_turns:
            return CallerAction("silence")

        # A low-confidence line makes the agent ask for a repeat; the lead then
        # repeats themselves more clearly.
        confidence = behaviour.asr_confidence
        if "say that once more" in (agent_text or "").lower() and self._last_line:
            self.repeated_last = True
            return CallerAction("speak", self._last_line, 0.95)

        text = self._line_for(state)
        self._last_line = text
        return CallerAction("speak", self._decorate(text), confidence)

    def _line_for(self, state: ConversationState) -> str:
        behaviour = self.behaviour

        if state in (ConversationState.GREETING, ConversationState.CONFIRM_PERSON):
            if not behaviour.is_owner:
                return behaviour.wrong_person_line
            return "Yes, speaking. Who is this?"

        if state is ConversationState.DISCOVERY:
            return behaviour.discovery_answer

        if state is ConversationState.PITCH:
            line = self._objection_line()
            if line:
                return line
            if behaviour.books_meeting:
                return "Okay, that sounds interesting. How does that work?"
            return "No thanks, we're not interested."

        if state is ConversationState.OBJECTION_HANDLING:
            self.rebuttals_heard += 1
            if not self._should_concede:
                line = self._objection_line()
                if line:
                    return line
                return "I'm still not convinced about that."
            self.conceded = True
            if behaviour.books_meeting:
                return "Okay, that makes sense."
            return "No thanks, not interested. Please don't call again."

        if state is ConversationState.CLOSE:
            if behaviour.books_meeting:
                return behaviour.preferred_slot
            return "No thanks, not interested."

        if state is ConversationState.BOOK_MEETING:
            return "Great, thank you."

        return "Okay, thank you."


PERSONA_SYSTEM = """\
You are role-playing a small business owner in South Delhi receiving a cold call.
Stay in character, answer in ONE short spoken sentence (max 20 words), never
mention that you are an AI or that this is a test.

Your character: {name} ({category}), running {business}.
Behaviour rules:
- You are {owner_status}.
- Objections you raise, in order: {objections}
- You accept a rebuttal after {concede_after} attempt(s).
- You {booking} agree to a 15-minute meeting when asked.
- If you agree, propose: "{slot}".
"""


class LLMCaller:
    """A second LLM plays the persona (slower, fuzzier, closer to real calls)."""

    def __init__(self, persona: Persona, llm: LLMClient) -> None:
        self.persona = persona
        self.llm = llm
        self.fallback = ScriptedCaller(persona)
        self.history: list[Message] = []
        self.turn = 0

    async def reply(self, agent_text: str, state: ConversationState) -> CallerAction:
        self.turn += 1
        behaviour = self.persona.behaviour
        if behaviour.hang_up_after_turn is not None and self.turn >= behaviour.hang_up_after_turn:
            return CallerAction("hangup")
        if self.turn in behaviour.silent_turns:
            return CallerAction("silence")

        system = PERSONA_SYSTEM.format(
            name=self.persona.name,
            category=self.persona.business.category,
            business=self.persona.business.name,
            owner_status="the owner" if behaviour.is_owner else "NOT the owner (staff member)",
            objections=", ".join(o.value for o in behaviour.objections) or "none",
            concede_after=behaviour.concede_after,
            booking="DO" if behaviour.books_meeting else "DO NOT",
            slot=behaviour.preferred_slot,
        )
        self.history.append(Message(role="user", content=f"Caller said: {agent_text}"))
        try:
            response = await self.llm.complete(
                [Message(role="system", content=system), *self.history],
                None,
                temperature=0.7,
                max_tokens=60,
            )
            text = (response.text or "").strip()
        except LLMError:
            return await self.fallback.reply(agent_text, state)
        if not text:
            return await self.fallback.reply(agent_text, state)
        self.history.append(Message(role="assistant", content=text))
        return CallerAction("speak", text, behaviour.asr_confidence)


def build_caller(persona: Persona, llm: LLMClient | None = None) -> ScriptedCaller | LLMCaller:
    return LLMCaller(persona, llm) if llm is not None else ScriptedCaller(persona)


__all__ = [
    "OBJECTION_LINES",
    "CallerAction",
    "LLMCaller",
    "ScriptedCaller",
    "build_caller",
]
