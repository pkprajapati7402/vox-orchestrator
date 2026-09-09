"""Persona definitions for the offline eval harness.

A persona is a *labelled test case*: how the business owner behaves, plus the
outcome and tool behaviour the agent is expected to produce. Because the labels
live next to the behaviour, the harness can score correctness rather than just
eyeballing transcripts.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.enums import CallOutcome, ObjectionType


@dataclass(frozen=True)
class Business:
    name: str
    category: str = "local business"
    area: str = "South Delhi"
    phone: str = "+911100000000"
    contact_name: str | None = None
    research_notes: str | None = None


@dataclass(frozen=True)
class Behaviour:
    """How the simulated lead reacts, turn by turn."""

    is_owner: bool = True
    wrong_person_line: str = "He's not here right now, I just work here."
    discovery_answer: str = "Mostly walk-ins, and a bit of Instagram."
    objections: tuple[ObjectionType, ...] = ()
    #: Rebuttals the lead listens to before accepting (or refusing) the pitch.
    concede_after: int = 1
    books_meeting: bool = True
    preferred_slot: str = "Thursday at 4 works"
    hang_up_after_turn: int | None = None
    silent_turns: tuple[int, ...] = ()
    asr_confidence: float = 0.93
    voicemail: bool = False
    rude: bool = False
    code_switching: bool = False


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    category: str
    expected_outcome: CallOutcome
    business: Business
    behaviour: Behaviour = field(default_factory=Behaviour)
    difficulty: str = "medium"
    #: Tools that MUST appear (in order) for the run to count as correct.
    required_tools: tuple[str, ...] = ()
    #: Tools that must never appear.
    forbidden_tools: tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["expected_outcome"] = self.expected_outcome.value
        payload["behaviour"]["objections"] = [o.value for o in self.behaviour.objections]
        return payload


def load_personas(path: str | Path | None = None) -> list[Persona]:
    """Load the built-in catalog, or a JSON file of persona overrides."""
    from eval.personas.catalog import PERSONAS

    if path is None:
        return list(PERSONAS)
    data = json.loads(Path(path).read_text())
    return [_from_dict(item) for item in data]


def _from_dict(item: dict[str, Any]) -> Persona:
    behaviour = dict(item.get("behaviour") or {})
    behaviour["objections"] = tuple(
        ObjectionType(value) for value in behaviour.get("objections", [])
    )
    behaviour["silent_turns"] = tuple(behaviour.get("silent_turns", []))
    return Persona(
        id=item["id"],
        name=item["name"],
        category=item["category"],
        expected_outcome=CallOutcome(item["expected_outcome"]),
        business=Business(**item["business"]),
        behaviour=Behaviour(**behaviour),
        difficulty=item.get("difficulty", "medium"),
        required_tools=tuple(item.get("required_tools", ())),
        forbidden_tools=tuple(item.get("forbidden_tools", ())),
        notes=item.get("notes", ""),
    )


def dump_personas(path: str | Path) -> int:
    """Write the built-in catalog to JSON (useful for editing/extending it)."""
    personas = load_personas()
    Path(path).write_text(json.dumps([p.to_dict() for p in personas], indent=2))
    return len(personas)


__all__ = ["Behaviour", "Business", "Persona", "dump_personas", "load_personas"]
