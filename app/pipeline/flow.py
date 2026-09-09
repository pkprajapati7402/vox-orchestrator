"""Pipecat Flows configuration derived from our own state machine.

`app/pipeline/engine.py` is the authoritative implementation of the state
machine (it must run without audio for the eval harness). This module projects
exactly the same states, prompts and tool schemas into the `FlowConfig` shape
that `pipecat-ai-flows` consumes, so the live pipeline and the offline harness
can never drift apart: both are generated from `TOOL_SPECS` + `STATE_PROMPTS`.

Building the config requires no pipecat import — it is a plain dict — which
keeps it unit-testable in CI where the voice extra is not installed.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.enums import ConversationState
from app.pipeline.engine import LeadContext
from app.pipeline.prompts import PROMPT_VERSION, build_system_prompt, state_prompt
from app.pipeline.states import successors
from app.tools.definitions import tools_for_state

#: Flows node names mirror the state values one-to-one.
NODE_NAMES: list[str] = [state.value for state in ConversationState]


def _node_functions(state: ConversationState) -> list[dict[str, Any]]:
    functions: list[dict[str, Any]] = []
    for spec in tools_for_state(state):
        schema = spec.json_schema()["function"]
        functions.append(
            {
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "handler": f"app.tools.registry:execute_tool::{schema['name']}",
                    "description": schema["description"],
                    "parameters": schema["parameters"],
                    # Flows uses this to know where a successful call can land.
                    "transition_to": sorted(s.value for s in successors(state)),
                },
            }
        )
    return functions


def build_flow_config(lead: LeadContext, *, prompt_variant: str = PROMPT_VERSION) -> dict[str, Any]:
    """Return a `FlowConfig`-shaped dict for this lead."""
    settings = get_settings()
    system = build_system_prompt(
        agent_name=settings.agent_name,
        agency_name=settings.agency_name,
        business_name=lead.business_name,
        category=lead.category,
        address=lead.address,
        contact_name=lead.contact_name,
        research_notes=lead.research_notes,
        variant=prompt_variant,
    )

    nodes: dict[str, Any] = {}
    for state in ConversationState:
        node: dict[str, Any] = {
            "role_messages": [{"role": "system", "content": system}],
            "task_messages": [{"role": "system", "content": state_prompt(state)}],
            "functions": _node_functions(state),
        }
        if state is ConversationState.WRAPUP:
            node["post_actions"] = [{"type": "end_conversation"}]
        nodes[state.value] = node

    return {"initial_node": ConversationState.GREETING.value, "nodes": nodes}


def flow_summary() -> dict[str, list[str]]:
    """`{state: [tool, ...]}` — used in tests and docs to show the machine."""
    return {
        state.value: sorted(spec.name for spec in tools_for_state(state))
        for state in ConversationState
    }


__all__ = ["NODE_NAMES", "build_flow_config", "flow_summary"]
