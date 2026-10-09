"""Deterministic LangGraph nodes.

The application, not the LLM, decides when memory is read and written. Each
node does one job, returns a partial state update and emits a progress event
for the UI through LangGraph's custom stream.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from langgraph.config import get_stream_writer

from app.agent import grounding, prompts, understanding
from app.agent.grounding import (
    grounded_reflect,
    match_explanation,
    memory_summary,
    parse_reflect,
)
from app.agent.state import AgentState
from app.core.config import Settings
from app.core.context import new_id
from app.core.hashing import sha256_bytes
from app.core.logging import get_logger
from app.db.pool import Connection, Database
from app.db.repositories import audit, conversations, deployments, files, incidents, observability, reference
from app.domain.recommendation import (
    ActionToAvoid,
    BaselineAnswer,
    Recommendation,
    RecommendedStep,
)
from app.guardrails.input_guard import (
    OFF_TOPIC_MESSAGE,
    check_topic,
    enforce_length,
    neutralise_injection,
    redact_sensitive,
)
from app.guardrails.output_guard import validate_safety
from app.guardrails.sanitize import (
    TreeReport,
    redact_tree,
    sanitize_baseline,
    sanitize_recommendation,
    tree_checks,
)
from app.llm.groq_client import GroqClient
from app.memory.client import HindsightGateway
from app.observability.tracing import trace_span, update_span
from app.storage.supabase_storage import SupabaseStorage

log = get_logger(__name__)

DEFAULT_CLARIFICATION = "Which service is affected, and what exact error message or alert are you seeing?"
MEMORY_UNAVAILABLE_NOTE = (
    "Hindsight memory was unavailable, so this answer comes from the language model alone and is not grounded "
    "in past incidents."
)

PROGRESS_LABELS = {
    "validate_input": "Validating input",
    "redact_sensitive_data": "Redacting secrets and personal data",
    "understand_incident": "Understanding the incident",
    "request_clarification": "Preparing a clarifying question",
    "fetch_recent_changes": "Checking recent production changes",
    "hindsight_reflect": "Reasoning over past incidents with Hindsight",
    "validate_evidence": "Validating evidence and citations",
    "validate_recommendations": "Checking recommendations for safety",
    "save_message_and_trace": "Saving the conversation",
    "return_response": "Done",
}


class AttachmentError(Exception):
    """An attachment failed ownership or integrity verification."""


@dataclass
class NodeDependencies:
    settings: Settings
    db: Database
    storage: SupabaseStorage
    hindsight: HindsightGateway
    groq: GroqClient


def emit(event: str, data: dict[str, Any]) -> None:
    get_stream_writer()({"event": event, "data": data})


def _progress(node: str, status: str = "running", **extra: Any) -> None:
    emit("progress", {"node": node, "label": PROGRESS_LABELS[node], "status": status, **extra})


async def _timed_tool[T](
    node: str, tool: str, call: Callable[[], Awaitable[T]]
) -> tuple[T | None, dict[str, Any]]:
    """Run a tool call and return (result, tool_call_record). Exceptions are captured, not raised."""
    started = time.perf_counter()
    try:
        result = await call()
    except Exception as error:
        record = {
            "node": node,
            "tool": tool,
            "status": "timeout" if isinstance(error, TimeoutError) else "error",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": str(error) if isinstance(error, AttachmentError) else type(error).__name__,
        }
        log.warning("tool_call_failed", agent_node=node, tool=tool, error_type=type(error).__name__)
        return None, record
    return result, {
        "node": node,
        "tool": tool,
        "status": "ok",
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


class AgentNodes:
    def __init__(self, deps: NodeDependencies) -> None:
        self.deps = deps

    # ------------------------------------------------------------------ 1. validate_input
    async def validate_input(self, state: AgentState) -> AgentState:
        _progress("validate_input")
        settings = self.deps.settings
        checks: list[dict[str, Any]] = []

        message = enforce_length(state["message"], max_chars=settings.max_message_chars)
        checks += [check.to_record() for check in message.checks]

        log_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        failures: list[str] = []
        for file_id in state.get("file_ids", []):
            content, record = await _timed_tool(
                "validate_input",
                "storage.download",
                partial(self._load_attachment, file_id, state["conversation_id"]),
            )
            tool_calls.append(record)
            if content is None:
                failures.append(f"{file_id} ({record.get('error') or 'unavailable'})")
            else:
                log_parts.append(content)

        if failures:
            # A failed attachment is reported to the engineer; the turn never continues without it.
            reason = (
                "These attachments could not be verified, so PagerMind did not analyse this message: "
                + ", ".join(failures)
                + ". Re-upload the log and try again."
            )
            emit("attachment_error", {"files": failures})
            return {
                "rejected": True,
                "rejection_reason": reason,
                "outcome": "rejected",
                "guardrail_checks": [
                    *checks,
                    {
                        "phase": "input",
                        "check_name": "attachment_integrity",
                        "decision": "reject",
                        "reason_code": "attachment_unverified",
                        "details": {"files": failures},
                    },
                ],
                "tool_calls": tool_calls,
                "step_count": 1,
                "graph_node": "validate_input",
            }

        log_text = enforce_length("\n".join(log_parts), max_chars=settings.max_log_chars)
        checks += [check.to_record() for check in log_text.checks]

        topic = check_topic(f"{message.text}\n{log_text.text}", set(state["known_services"]))
        update: AgentState = {
            "message": message.text,
            "log_text": log_text.text,
            "rejected": topic is not None,
            "rejection_reason": OFF_TOPIC_MESSAGE if topic else None,
            "guardrail_checks": checks + ([topic.to_record()] if topic else []),
            "tool_calls": tool_calls,
            "step_count": 1,
            "graph_node": "validate_input",
        }
        if topic:
            update["outcome"] = "rejected"
        return update

    async def _load_attachment(self, file_id: str, conversation_id: str) -> str:
        """Download an attachment only if it belongs to this conversation and matches its manifest."""
        async with self.deps.db.transaction() as conn:
            manifest = await files.get_manifest(conn, file_id)
        if manifest is None or manifest["conversation_id"] != conversation_id:
            raise AttachmentError("not_attached_to_conversation")
        if manifest["redaction_state"] == "rejected":
            raise AttachmentError("rejected_file")
        data = await self.deps.storage.download(manifest["storage_bucket"], manifest["storage_path"])
        if len(data) != manifest["size_bytes"]:
            raise AttachmentError("size_mismatch")
        if sha256_bytes(data) != manifest["sha256"]:
            raise AttachmentError("checksum_mismatch")
        return data.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------ 2. redact_sensitive_data
    async def redact_sensitive_data(self, state: AgentState) -> AgentState:
        _progress("redact_sensitive_data")
        checks: list[dict[str, Any]] = []

        def clean(text: str) -> str:
            redacted = redact_sensitive(text)
            neutralised = neutralise_injection(redacted.text)
            checks.extend(check.to_record() for check in (*redacted.checks, *neutralised.checks))
            return neutralised.text

        return {
            "message": clean(state["message"]),
            "log_text": clean(state.get("log_text", "")),
            "guardrail_checks": checks,
            "step_count": 1,
            "graph_node": "redact_sensitive_data",
        }

    # ------------------------------------------------------------------ 3. understand_incident
    async def understand_incident(self, state: AgentState) -> AgentState:
        _progress("understand_incident")
        known = set(state["known_services"])
        prior = state.get("prior") or {}
        log_text = state.get("log_text", "")

        result, record = await _timed_tool(
            "understand_incident",
            "groq.understand",
            lambda: understanding.ask_model(
                self.deps.groq, message=state["message"], log_text=log_text, known_services=known, prior=prior
            ),
        )
        resolved = understanding.resolve(
            result.value if result else None,
            message=state["message"],
            log_text=log_text,
            known_services=known,
            prior=prior,
        )
        service_id, signatures = resolved.service_id, resolved.error_signatures
        needs_clarification = resolved.missing_context and not state.get("clarification_already_asked", False)

        update: AgentState = {
            "service_id": service_id,
            "severity": resolved.severity,
            "symptoms": resolved.symptoms,
            "error_signatures": signatures,
            "needs_clarification": needs_clarification,
            "clarification_question": resolved.clarification_question if needs_clarification else None,
            "tool_calls": [record],
            "step_count": 1,
            "graph_node": "understand_incident",
        }
        if result:
            update["input_tokens"] = result.input_tokens or 0
            update["output_tokens"] = result.output_tokens or 0
        _progress("understand_incident", "done", service_id=service_id, signatures=len(signatures))
        return update

    # ------------------------------------------------------------------ 4. request_clarification
    async def request_clarification(self, state: AgentState) -> AgentState:
        _progress("request_clarification")
        question = state.get("clarification_question") or DEFAULT_CLARIFICATION
        return {
            "clarification_question": question,
            "outcome": "clarification",
            "step_count": 1,
            "graph_node": "request_clarification",
        }

    # ------------------------------------------------------------------ 5. fetch_recent_changes
    async def fetch_recent_changes(self, state: AgentState) -> AgentState:
        _progress("fetch_recent_changes")
        service_id = state.get("service_id")
        if not service_id:
            return {"recent_changes": [], "step_count": 1, "graph_node": "fetch_recent_changes"}

        async def _query() -> list[dict[str, Any]]:
            async with self.deps.db.transaction() as conn:
                related = [service_id, *await reference.service_dependencies(conn, service_id)]
                return await deployments.recent_changes(
                    conn, service_ids=related, before=datetime.now(UTC), window=timedelta(hours=24)
                )

        changes, record = await _timed_tool("fetch_recent_changes", "supabase.recent_changes", _query)
        changes = changes or []
        emit("recent_changes", {"changes": changes})
        return {
            "recent_changes": changes,
            "tool_calls": [record],
            "step_count": 1,
            "graph_node": "fetch_recent_changes",
        }

    # ------------------------------------------------------------------ 6. hindsight_reflect
    async def hindsight_reflect(self, state: AgentState) -> AgentState:
        """Reflect (memory-grounded answer) and the plain-LLM baseline run concurrently."""
        _progress("hindsight_reflect")
        # log_text was already redacted and neutralised by redact_sensitive_data.
        log_excerpt = "\n".join(state.get("log_text", "").splitlines()[-grounding.LOG_EXCERPT_LINES :])

        async def _reflect() -> Any:
            budget = grounding.reflect_budget(state.get("severity"))
            span_input = {"service_id": state.get("service_id"), "budget": budget}
            with trace_span("hindsight.reflect", input=span_input) as span:
                result = await grounded_reflect(
                    self.deps.hindsight,
                    service_id=state.get("service_id"),
                    severity=state.get("severity"),
                    symptoms=state["symptoms"],
                    error_signatures=state["error_signatures"],
                    recent_changes=state.get("recent_changes", []),
                    log_text=log_excerpt,
                )
                update_span(
                    span, output={"facts": len(result.facts), "structured": result.structured is not None}
                )
                return result

        async def _baseline() -> Any:
            return await self.deps.groq.complete_json(
                name="baseline_without_memory",
                system=prompts.BASELINE_SYSTEM,
                user=prompts.baseline_prompt(message=state["message"], log_text=log_excerpt),
                schema=BaselineAnswer,
            )

        (reflected, reflect_record), (baseline, baseline_record) = await asyncio.gather(
            _timed_tool("hindsight_reflect", "hindsight.reflect", _reflect),
            _timed_tool("hindsight_reflect", "groq.baseline", _baseline),
        )

        input_tokens = 0
        output_tokens = 0
        update: AgentState = {
            "memory_available": reflected is not None,
            "tool_calls": [reflect_record, baseline_record],
            "step_count": 1,
            "graph_node": "hindsight_reflect",
            "baseline": None,
        }
        if baseline:
            safe_baseline, baseline_checks = sanitize_baseline(baseline.value)
            update["baseline"] = safe_baseline.model_dump()
            update["guardrail_checks"] = [check.to_record() for check in baseline_checks]
            update["model"] = baseline.model
            update["fallback_used"] = baseline.fallback_used
            input_tokens += baseline.input_tokens or 0
            output_tokens += baseline.output_tokens or 0
            emit("baseline", {"baseline": update["baseline"], "model": baseline.model})
        if reflected:
            update["reflect_structured"] = reflected.structured
            update["reflect_text"] = redact_tree(reflected.text)
            update["facts"] = redact_tree(grounding.evidence_dicts(reflected))
            input_tokens += reflected.input_tokens or 0
            output_tokens += reflected.output_tokens or 0
        else:
            update["last_error"] = reflect_record.get("error")
        update["input_tokens"] = input_tokens
        update["output_tokens"] = output_tokens
        _progress("hindsight_reflect", "done", memory_available=reflected is not None)
        return update

    # ------------------------------------------------------------------ 7. validate_evidence
    async def validate_evidence(self, state: AgentState) -> AgentState:
        _progress("validate_evidence")
        if not state.get("memory_available"):
            recommendation = self._fallback_recommendation(state.get("baseline"))
            return {
                "recommendation": recommendation.model_dump(),
                "match_strength": "unavailable",
                "match_explanation": match_explanation(
                    "unavailable",
                    recommendation=recommendation,
                    facts=[],
                    error_signatures=[],
                    facts_per_incident=Counter(),
                    strong_match_min_facts=self.deps.settings.strong_match_min_facts,
                ),
                "memory_summary": memory_summary([]),
                "retrieved_incident_ids": [],
                "outcome": "memory_unavailable",
                "step_count": 1,
                "graph_node": "validate_evidence",
            }

        facts = state.get("facts", [])
        retrieved = grounding.incident_support(facts)[1]
        recommendation, parse_check = parse_reflect(
            state.get("reflect_structured"), state.get("reflect_text")
        )

        async with self.deps.db.transaction() as conn:
            known = await incidents.existing_incident_ids(conn, recommendation.cited_ids | retrieved)

        min_facts = self.deps.settings.strong_match_min_facts
        grounded = grounding.ground(
            recommendation, facts=facts, known_incident_ids=known, strong_match_min_facts=min_facts
        )
        checks = [check.to_record() for check in grounded.checks] + ([parse_check] if parse_check else [])
        explanation = match_explanation(
            grounded.match_strength,
            recommendation=grounded.recommendation,
            facts=facts,
            error_signatures=state.get("error_signatures", []),
            facts_per_incident=grounded.facts_per_incident,
            strong_match_min_facts=min_facts,
        )
        return {
            "recommendation": grounded.recommendation.model_dump(),
            "match_strength": grounded.match_strength,
            "match_explanation": explanation,
            "memory_summary": memory_summary(facts),
            "retrieved_incident_ids": grounded.retrieved_incident_ids,
            "guardrail_checks": checks,
            "outcome": "answered",
            "step_count": 1,
            "graph_node": "validate_evidence",
        }

    @staticmethod
    def _fallback_recommendation(baseline: dict[str, Any] | None) -> Recommendation:
        if not baseline:
            return Recommendation(
                summary="PagerMind could not reach its memory or language model. Escalate to the service owner.",
                is_strong_match=False,
                confidence=0.0,
                abstention_reason=MEMORY_UNAVAILABLE_NOTE,
            )
        answer = BaselineAnswer.model_validate(baseline)
        return Recommendation(
            summary=answer.summary,
            is_strong_match=False,
            likely_cause=answer.likely_cause,
            confidence=0.3,
            recommended_steps=[
                RecommendedStep(**{**step.model_dump(), "evidence_ids": [], "is_hypothesis": True})
                for step in answer.recommended_steps
            ],
            actions_to_avoid=[
                ActionToAvoid(**{**a.model_dump(), "evidence_ids": []}) for a in answer.actions_to_avoid
            ],
            abstention_reason=MEMORY_UNAVAILABLE_NOTE,
        )

    # ------------------------------------------------------------------ 8. validate_recommendations
    async def validate_recommendations(self, state: AgentState) -> AgentState:
        _progress("validate_recommendations")
        recommendation = Recommendation.model_validate(state["recommendation"])
        team = None
        service_id = state.get("service_id")
        if service_id:
            async with self.deps.db.transaction() as conn:
                team = await reference.escalation_team(conn, service_id)
        result = validate_safety(recommendation, escalation_team=team)
        rec = result.recommendation
        rec.escalation_team = rec.escalation_team or team

        # Final pass over the complete object: every string is redacted and re-checked for citations.
        report = TreeReport()
        cleaned = sanitize_recommendation(
            rec.model_dump(), allowed_incident_ids=set(state.get("retrieved_incident_ids", [])), report=report
        )
        checks = [*result.checks, *tree_checks(report, scope="recommendation")]
        return {
            "recommendation": Recommendation.model_validate(cleaned).model_dump(),
            "guardrail_checks": [check.to_record() for check in checks],
            "step_count": 1,
            "graph_node": "validate_recommendations",
        }

    # ------------------------------------------------------------------ 9. save_message_and_trace
    async def save_message_and_trace(self, state: AgentState) -> AgentState:
        _progress("save_message_and_trace")
        outcome = state.get("outcome", "answered")
        message_id = new_id("MSG")
        content, message_type = self._message_content(state)
        incident_id = None

        async with self.deps.db.transaction() as conn:
            conversation = await conversations.get_conversation(conn, state["conversation_id"])
            incident_id = conversation["incident_id"] if conversation else None
            if (
                incident_id is None
                and outcome in ("answered", "memory_unavailable")
                and state.get("service_id")
            ):
                incident_id = await self._open_live_incident(conn, state)
            await conversations.attach_context(
                conn, state["conversation_id"], service_id=state.get("service_id"), incident_id=incident_id
            )
            await conversations.add_message(
                conn,
                message_id=message_id,
                conversation_id=state["conversation_id"],
                role="agent",
                message_type=message_type,
                content=content,
                payload=redact_tree(self._message_payload(state)),
                request_id=state["request_id"],
            )
            await conversations.upsert_checkpoint(
                conn,
                state["conversation_id"],
                {
                    "incident_id": incident_id,
                    "service_id": state.get("service_id"),
                    "symptoms": state.get("symptoms"),
                    "error_signatures": state.get("error_signatures", []),
                    "recent_changes": state.get("recent_changes", []),
                    "matched_incidents": [
                        m["incident_id"]
                        for m in (state.get("recommendation") or {}).get("matched_incidents", [])
                    ],
                    "graph_node": "save_message_and_trace",
                    "step_count": state.get("step_count", 0) + 1,
                    "recommendation": state.get("recommendation"),
                    "last_error": state.get("last_error"),
                },
            )
            await conversations.add_tool_calls(conn, state["run_id"], state.get("tool_calls", []))
            await observability.insert_guardrail_results(
                conn, state["request_id"], state.get("guardrail_checks", [])
            )
            await audit.record(
                conn,
                "alert.processed",
                resource_type="conversation",
                resource_id=state["conversation_id"],
                actor_type="engineer",
                actor_id=state["engineer_id"],
                details={"outcome": outcome, "match_strength": state.get("match_strength")},
            )
            if any(call["tool"] == "hindsight.reflect" for call in state.get("tool_calls", [])):
                await audit.record(
                    conn,
                    "hindsight.called",
                    resource_type="agent_run",
                    resource_id=state["run_id"],
                    details={
                        "memory_available": state.get("memory_available", False),
                        "facts": len(state.get("facts", [])),
                    },
                )
            if any(
                check.get("reason_code") in ("secret_detected", "pii_detected")
                for check in state.get("guardrail_checks", [])
            ):
                await audit.record(
                    conn, "redaction.performed", resource_type="message", resource_id=message_id
                )

        return {
            "message_id": message_id,
            "incident_id": incident_id,
            "outcome": outcome,
            "step_count": 1,
            "graph_node": "save_message_and_trace",
        }

    async def _open_live_incident(self, conn: Connection, state: AgentState) -> str:
        incident_id = f"INC-L{uuid.uuid4().hex[:8].upper()}"
        signatures = state.get("error_signatures", [])
        await incidents.create_live_incident(
            conn,
            incident_id=incident_id,
            service_id=state["service_id"],  # type: ignore[arg-type]
            title=f"{state['service_id']}: {state.get('symptoms', '')[:120]}",
            severity=state.get("severity") or "SEV3",
            alert={"title": state.get("symptoms", "")[:200], "value": "", "error_signatures": signatures},
            started_at=datetime.now(UTC),
        )
        await audit.record(
            conn,
            "incident.opened",
            resource_type="incident",
            resource_id=incident_id,
            actor_type="engineer",
            actor_id=state["engineer_id"],
        )
        return incident_id

    @staticmethod
    def _message_content(state: AgentState) -> tuple[str, str]:
        outcome = state.get("outcome", "answered")
        if outcome == "rejected":
            return state.get("rejection_reason") or OFF_TOPIC_MESSAGE, "system_event"
        if outcome == "clarification":
            return state.get("clarification_question") or DEFAULT_CLARIFICATION, "agent_response"
        recommendation = state.get("recommendation") or {}
        return recommendation.get("summary", ""), "agent_response"

    @staticmethod
    def _message_payload(state: AgentState) -> dict[str, Any]:
        return {
            "outcome": state.get("outcome"),
            "recommendation": state.get("recommendation"),
            "baseline": state.get("baseline"),
            "match_strength": state.get("match_strength"),
            "match_explanation": state.get("match_explanation"),
            "memory_summary": state.get("memory_summary"),
            "memory_available": state.get("memory_available"),
            "evidence": state.get("facts", []),
            "recent_changes": state.get("recent_changes", []),
            "error_signatures": state.get("error_signatures", []),
            "clarification_question": state.get("clarification_question"),
            "guardrails": [
                {"check": c["check_name"], "decision": c["decision"], "reason": c.get("reason_code")}
                for c in state.get("guardrail_checks", [])
                if c["decision"] != "allow"
            ],
        }

    # ------------------------------------------------------------------ 10. return_response
    async def return_response(self, state: AgentState) -> AgentState:
        _progress("return_response", "done")
        return {"step_count": 1, "graph_node": "return_response"}
