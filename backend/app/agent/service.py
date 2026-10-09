"""Runs one chat turn through the graph and turns it into Server-Sent Events."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from datetime import timedelta
from typing import Any

from app.agent.graph import build_graph
from app.agent.nodes import NodeDependencies
from app.agent.state import AgentState
from app.core.config import Settings
from app.core.context import current_request_id, new_id, new_request_id
from app.core.errors import NotFoundError, ValidationFailed
from app.core.logging import get_logger
from app.core.redaction import redact_text
from app.db.pool import Database
from app.db.repositories import audit, conversations, files, reference
from app.domain.contracts import ChatRequest
from app.guardrails.sanitize import redact_tree
from app.llm.groq_client import GroqClient
from app.memory.client import HindsightGateway
from app.observability.sentry import capture
from app.observability.tracing import trace_request
from app.storage.supabase_storage import SupabaseStorage

log = get_logger(__name__)

SSEEvent = dict[str, str]


def _sse(event: str, data: dict[str, Any]) -> SSEEvent:
    return {"event": event, "data": json.dumps(data, default=str)}


class AgentService:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        storage: SupabaseStorage,
        hindsight: HindsightGateway,
        groq: GroqClient,
    ) -> None:
        self.settings = settings
        self.db = db
        self._slots = asyncio.Semaphore(settings.max_concurrent_agent_runs)
        self.graph = build_graph(
            NodeDependencies(settings=settings, db=db, storage=storage, hindsight=hindsight, groq=groq)
        )

    @property
    def at_capacity(self) -> bool:
        """True when this instance is already running its maximum number of chat turns."""
        return self._slots.locked()

    async def stream(self, request: ChatRequest) -> AsyncIterator[SSEEvent]:
        """One chat turn. A semaphore bounds concurrent turns so load cannot exhaust the DB pool."""
        # aclosing() propagates a client disconnect into the inner generator so it is recorded.
        async with self._slots, aclosing(self._run(request)) as events:
            async for event in events:
                yield event

    async def _run(self, request: ChatRequest) -> AsyncGenerator[SSEEvent]:
        request_id = current_request_id() or new_request_id()
        run_id = new_id("RUN")
        started = time.perf_counter()

        try:
            initial = await self._prepare(request, request_id=request_id, run_id=run_id)
        except (NotFoundError, ValidationFailed) as error:
            yield _sse(
                "error",
                {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                    "request_id": request_id,
                },
            )
            return

        conversation_id = initial["conversation_id"]
        yield _sse(
            "conversation", {"conversation_id": conversation_id, "run_id": run_id, "request_id": request_id}
        )

        final: AgentState = initial
        trace_id: str | None = None
        try:
            with trace_request(
                name="chat_turn",
                request_id=request_id,
                conversation_id=conversation_id,
                metadata={"engineer_id": request.engineer_id},
            ) as trace_id:
                async with asyncio.timeout(self.settings.request_timeout_seconds):
                    async for mode, chunk in self.graph.astream(
                        initial,
                        config={
                            "recursion_limit": self.settings.agent_max_steps,
                            "metadata": {"request_id": request_id},
                        },
                        stream_mode=["custom", "values"],
                    ):
                        if mode == "custom":
                            yield _sse(chunk["event"], redact_tree(chunk["data"]))
                        else:
                            final = chunk
        except (asyncio.CancelledError, GeneratorExit):
            # The client disconnected. Record it without blocking the cancellation.
            await asyncio.shield(
                self._finish_run(run_id, final, started, status="failed", error="client_disconnected")
            )
            raise
        except Exception as error:
            log.exception(
                "agent_run_failed", conversation_id=conversation_id, error_type=type(error).__name__
            )
            capture(error, component="agent")
            await self._finish_run(run_id, final, started, status="failed", error=type(error).__name__)
            yield _sse(
                "error",
                {
                    "code": "agent_failed",
                    "message": "PagerMind could not complete this request.",
                    "request_id": request_id,
                },
            )
            return

        # Bookkeeping never prevents a completed answer from reaching the engineer.
        status = {"rejected": "blocked", "clarification": "clarification"}.get(
            final.get("outcome", ""), "succeeded"
        )
        await self._finish_run(run_id, final, started, status=status)
        await self._link_trace(request_id, conversation_id, run_id, trace_id)

        for event in self._final_events(final):
            yield event
        yield _sse(
            "done",
            {
                "conversation_id": conversation_id,
                "message_id": final.get("message_id"),
                "incident_id": final.get("incident_id"),
                "run_id": run_id,
                "request_id": request_id,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            },
        )

    # ------------------------------------------------------------------ setup
    async def _prepare(self, request: ChatRequest, *, request_id: str, run_id: str) -> AgentState:
        redacted = redact_text(request.message)
        async with self.db.transaction() as conn:
            if not await reference.engineer_exists(conn, request.engineer_id):
                raise ValidationFailed("Unknown engineer.")

            conversation_id = request.conversation_id
            prior: dict[str, Any] = {}
            clarification_asked = False
            if conversation_id:
                conversation = await conversations.get_conversation(conn, conversation_id)
                if conversation is None:
                    raise NotFoundError("Conversation not found.")
                checkpoint = conversation.get("checkpoint") or {}
                prior = {
                    key: checkpoint.get(key)
                    for key in ("service_id", "symptoms", "error_signatures")
                    if checkpoint.get(key)
                }
                # Ask at most one clarifying question in a row; afterwards proceed with what we have.
                last_message = conversation["messages"][-1] if conversation["messages"] else {}
                clarification_asked = (last_message.get("payload") or {}).get("outcome") == "clarification"
            else:
                conversation_id = new_id("CONV")
                await conversations.create_conversation(
                    conn,
                    conversation_id=conversation_id,
                    engineer_id=request.engineer_id,
                    title=redacted.text[:120],
                )
                await audit.record(
                    conn,
                    "conversation.created",
                    resource_type="conversation",
                    resource_id=conversation_id,
                    actor_type="engineer",
                    actor_id=request.engineer_id,
                )

            claimed = await files.claim_uploads(
                conn,
                request.file_ids,
                conversation_id=conversation_id,
                engineer_id=request.engineer_id,
                ttl=timedelta(minutes=self.settings.upload_claim_ttl_minutes),
            )
            unclaimed = sorted(set(request.file_ids) - claimed)
            if unclaimed:
                # Rolls back the conversation and message created above.
                raise ValidationFailed(
                    "Some attachments cannot be used in this conversation. Upload them again.",
                    details={"file_ids": unclaimed},
                )

            await conversations.add_message(
                conn,
                message_id=new_id("MSG"),
                conversation_id=conversation_id,
                role="engineer",
                message_type="engineer_input",
                content=redacted.text,
                engineer_id=request.engineer_id,
                payload={"file_ids": request.file_ids} if request.file_ids else None,
                redaction_status="redacted" if redacted.changed else "passed",
                request_id=request_id,
            )
            await conversations.start_run(
                conn, run_id=run_id, conversation_id=conversation_id, request_id=request_id
            )
            known_services = sorted(await reference.service_ids(conn))

        return {
            "request_id": request_id,
            "run_id": run_id,
            "conversation_id": conversation_id,
            "engineer_id": request.engineer_id,
            "message": request.message,
            "file_ids": request.file_ids,
            "known_services": known_services,
            "prior": prior,
            "clarification_already_asked": clarification_asked,
            "guardrail_checks": [],
            "tool_calls": [],
            "step_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }

    # ------------------------------------------------------------------ results
    @staticmethod
    def _final_events(state: AgentState) -> list[SSEEvent]:
        """Final payloads pass through recursive redaction as the last line of defence."""
        return [_sse(name, redact_tree(payload)) for name, payload in AgentService._final_payloads(state)]

    @staticmethod
    def _final_payloads(state: AgentState) -> list[tuple[str, dict[str, Any]]]:
        outcome = state.get("outcome")
        if outcome == "rejected":
            return [("rejected", {"reason": state.get("rejection_reason")})]
        if outcome == "clarification":
            return [
                (
                    "clarification",
                    {"question": state.get("clarification_question"), "message_id": state.get("message_id")},
                )
            ]
        return [
            (
                "recommendation",
                {
                    "message_id": state.get("message_id"),
                    "incident_id": state.get("incident_id"),
                    "recommendation": state.get("recommendation"),
                    "match_strength": state.get("match_strength"),
                    "match_explanation": state.get("match_explanation"),
                    "memory_summary": state.get("memory_summary"),
                    "memory_available": state.get("memory_available", False),
                    "evidence": state.get("facts", []),
                    "retrieved_incident_ids": state.get("retrieved_incident_ids", []),
                    "error_signatures": state.get("error_signatures", []),
                    "service_id": state.get("service_id"),
                },
            )
        ]

    async def _finish_run(
        self, run_id: str, state: AgentState, started: float, *, status: str, error: str | None = None
    ) -> None:
        try:
            async with self.db.transaction() as conn:
                await conversations.finish_run(
                    conn,
                    run_id,
                    {
                        "message_id": state.get("message_id"),
                        "status": status,
                        "outcome": state.get("outcome"),
                        "match_strength": state.get("match_strength"),
                        "memory_available": state.get("memory_available"),
                        "model": state.get("model"),
                        "fallback_used": state.get("fallback_used", False),
                        "input_tokens": state.get("input_tokens") or None,
                        "output_tokens": state.get("output_tokens") or None,
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                        "error": error or state.get("last_error"),
                    },
                )
        except Exception as db_error:  # never mask the original failure
            log.error("finish_run_failed", run_id=run_id, error_type=type(db_error).__name__)

    async def _link_trace(
        self, request_id: str, conversation_id: str, run_id: str, trace_id: str | None
    ) -> None:
        try:
            async with self.db.transaction() as conn:
                await conversations.link_trace(
                    conn,
                    request_id=request_id,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    langfuse_trace_id=trace_id,
                )
        except Exception as error:
            log.error("trace_link_failed", run_id=run_id, error_type=type(error).__name__)
