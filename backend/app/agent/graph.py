"""LangGraph wiring.

validate_input ─┬─ (off-topic) ───────────────────────────────────────────────┐
                └─ redact_sensitive_data → understand_incident ─┬─ request_clarification ─┤
                                                               └─ fetch_recent_changes   │
                   → hindsight_reflect → validate_evidence → validate_recommendations ──┤
                                                                                        ▼
                                                 save_message_and_trace → return_response → END
"""

from __future__ import annotations

import time
from typing import Any, Literal, Protocol

from langgraph.graph import END, START, StateGraph

from app.agent.nodes import AgentNodes, NodeDependencies
from app.agent.state import AgentState
from app.core.logging import get_logger
from app.observability.tracing import trace_span, update_span

log = get_logger("agent")


class Node(Protocol):
    async def __call__(self, state: AgentState) -> AgentState: ...


def _instrumented(name: str, node: Node) -> Node:
    """One Langfuse span and one structured log line per node execution."""

    async def run(state: AgentState) -> AgentState:
        started = time.perf_counter()
        with trace_span(f"node.{name}", metadata={"conversation_id": state.get("conversation_id")}) as span:
            try:
                update = await node(state)
            except Exception as error:
                update_span(span, level="ERROR", metadata={"error_type": type(error).__name__})
                log.error(
                    "node_failed",
                    agent_node=name,
                    conversation_id=state.get("conversation_id"),
                    error_type=type(error).__name__,
                )
                raise
            duration_ms = int((time.perf_counter() - started) * 1000)
            update_span(
                span, output={"outcome": update.get("outcome")}, metadata={"duration_ms": duration_ms}
            )
        log.info(
            "node_completed",
            agent_node=name,
            conversation_id=state.get("conversation_id"),
            incident_id=update.get("incident_id") or state.get("incident_id"),
            service=update.get("service_id") or state.get("service_id"),
            duration_ms=duration_ms,
            outcome=update.get("outcome"),
        )
        return update

    return run


def _after_validation(state: AgentState) -> Literal["redact_sensitive_data", "save_message_and_trace"]:
    return "save_message_and_trace" if state.get("rejected") else "redact_sensitive_data"


def _after_understanding(state: AgentState) -> Literal["request_clarification", "fetch_recent_changes"]:
    return "request_clarification" if state.get("needs_clarification") else "fetch_recent_changes"


def build_graph(deps: NodeDependencies) -> Any:
    nodes = AgentNodes(deps)
    graph = StateGraph(AgentState)

    for name in (
        "validate_input",
        "redact_sensitive_data",
        "understand_incident",
        "request_clarification",
        "fetch_recent_changes",
        "hindsight_reflect",
        "validate_evidence",
        "validate_recommendations",
        "save_message_and_trace",
        "return_response",
    ):
        graph.add_node(name, _instrumented(name, getattr(nodes, name)))

    graph.add_edge(START, "validate_input")
    graph.add_conditional_edges("validate_input", _after_validation)
    graph.add_edge("redact_sensitive_data", "understand_incident")
    graph.add_conditional_edges("understand_incident", _after_understanding)
    graph.add_edge("request_clarification", "save_message_and_trace")
    graph.add_edge("fetch_recent_changes", "hindsight_reflect")
    graph.add_edge("hindsight_reflect", "validate_evidence")
    graph.add_edge("validate_evidence", "validate_recommendations")
    graph.add_edge("validate_recommendations", "save_message_and_trace")
    graph.add_edge("save_message_and_trace", "return_response")
    graph.add_edge("return_response", END)

    return graph.compile()
