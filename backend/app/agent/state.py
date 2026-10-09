"""LangGraph state for one chat turn."""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

Outcome = Literal["answered", "clarification", "rejected", "memory_unavailable", "failed"]


class AgentState(TypedDict, total=False):
    # Request
    request_id: str
    run_id: str
    conversation_id: str
    engineer_id: str
    message: str
    file_ids: list[str]
    known_services: list[str]

    # Context carried over from the previous turn (agent_checkpoints)
    prior: dict[str, Any]
    clarification_already_asked: bool

    # Input
    log_text: str
    rejected: bool
    rejection_reason: str | None

    # Understanding
    service_id: str | None
    severity: str | None
    symptoms: str
    error_signatures: list[str]
    needs_clarification: bool
    clarification_question: str | None

    # Context and memory
    recent_changes: list[dict[str, Any]]
    memory_available: bool
    reflect_structured: dict[str, Any] | None
    reflect_text: str
    facts: list[dict[str, Any]]
    baseline: dict[str, Any] | None

    # Validated answer
    recommendation: dict[str, Any] | None
    match_strength: Literal["strong", "weak", "none", "unavailable"]
    retrieved_incident_ids: list[str]
    match_explanation: str
    memory_summary: dict[str, Any]

    # Bookkeeping (reducers accumulate across nodes)
    guardrail_checks: Annotated[list[dict[str, Any]], operator.add]
    tool_calls: Annotated[list[dict[str, Any]], operator.add]
    step_count: Annotated[int, operator.add]
    input_tokens: Annotated[int, operator.add]
    output_tokens: Annotated[int, operator.add]
    model: str | None
    fallback_used: bool
    last_error: str | None

    # Persistence results
    incident_id: str | None
    message_id: str | None
    outcome: Outcome
    graph_node: str
