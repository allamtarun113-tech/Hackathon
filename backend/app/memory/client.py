"""Async Hindsight gateway used by the agent, the memory worker and the setup CLI.

All Hindsight traffic goes through this class so timeouts, retries, circuit
breaking and response normalisation live in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import aiohttp
from hindsight_client import Hindsight
from hindsight_client_api.exceptions import ApiException
from hindsight_client_api.models.create_webhook_request import CreateWebhookRequest

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.resilience import BreakerStore, CircuitBreaker, call_with_resilience
from app.memory.bank import DISPOSITION, REFLECT_SCOPE_TAGS, BankConfig

log = get_logger(__name__)

WEBHOOK_EVENTS = ["retain.completed", "memory_defense.triggered"]
PLAN_LIMIT_STATUSES = frozenset({402, 403})
Budget = Literal["low", "mid", "high"]


def is_permanent_rejection(error: BaseException) -> bool:
    """Hindsight refused the request itself (4xx other than rate limiting): retrying cannot succeed."""
    status = getattr(error, "status", None)
    return (
        isinstance(error, (ApiException, aiohttp.ClientResponseError))
        and status is not None
        and (400 <= status < 500 and status not in (408, 429))
    )


def _is_retryable(error: BaseException) -> bool:
    if isinstance(error, ApiException):
        return error.status is None or error.status >= 500 or error.status == 429
    return isinstance(error, (ConnectionError, OSError))


@dataclass(frozen=True)
class MemoryFact:
    fact_id: str | None
    text: str
    document_id: str | None
    incident_id: str | None
    occurred_at: str | None
    tags: list[str] = field(default_factory=list)
    score: float | None = None
    # Hindsight fact type: "world" / "experience" are extracted facts, "observation" is consolidated
    # knowledge across documents (e.g. "restarting payments-api pods never fixed pool exhaustion").
    fact_type: str | None = None
    # Our own memory type from the `memory_type:*` tag: incident / feedback / procedure.
    memory_type: str | None = None


@dataclass(frozen=True)
class ReflectResult:
    structured: dict[str, Any] | None
    structured_error: str | None
    text: str
    facts: list[MemoryFact]
    input_tokens: int | None
    output_tokens: int | None
    # Mental models Hindsight consulted, as facts with fact_type="mental_model" (no incident attribution).
    mental_models: list[MemoryFact] = field(default_factory=list)


@dataclass(frozen=True)
class StoredDocument:
    document_id: str
    tags: list[str]
    memory_unit_count: int


@dataclass(frozen=True)
class BankStats:
    documents: int
    observations: int
    pending_operations: int
    pending_consolidation: int
    failed_consolidation: int


@dataclass(frozen=True)
class OperationStatus:
    operation_id: str
    status: str
    error_message: str | None
    memory_unit_count: int | None


def _incident_id(document_id: str | None, metadata: dict[str, str] | None) -> str | None:
    if metadata and metadata.get("incident_id"):
        return metadata["incident_id"]
    if document_id and document_id.startswith("incident:"):
        return document_id.removeprefix("incident:")
    return None


def _memory_type(tags: list[str], document_id: str | None) -> str | None:
    for value in tags:
        if value.startswith("memory_type:"):
            return value.removeprefix("memory_type:")
    prefix = (document_id or "").split(":", 1)[0]
    return {"incident": "incident", "feedback": "feedback", "runbook": "procedure"}.get(prefix)


def _to_mental_model(raw: Any) -> MemoryFact:
    return MemoryFact(
        fact_id=raw.id,
        text=raw.text,
        document_id=None,
        incident_id=None,
        occurred_at=None,
        fact_type="mental_model",
    )


def _to_fact(raw: Any) -> MemoryFact:
    scores = getattr(raw, "scores", None)
    tags = list(raw.tags or [])
    return MemoryFact(
        fact_id=getattr(raw, "id", None),
        text=raw.text,
        document_id=raw.document_id,
        incident_id=_incident_id(raw.document_id, raw.metadata),
        occurred_at=raw.occurred_start or getattr(raw, "mentioned_at", None),
        tags=tags,
        score=getattr(scores, "final", None) if scores is not None else None,
        fact_type=getattr(raw, "type", None),
        memory_type=_memory_type(tags, raw.document_id),
    )


class HindsightGateway:
    def __init__(
        self, settings: Settings, *, bank_id: str | None = None, breaker_store: BreakerStore | None = None
    ) -> None:
        self._settings = settings
        self.bank_id = bank_id or settings.hindsight_bank_id
        self._client = Hindsight(
            base_url=settings.hindsight_base_url,
            api_key=settings.hindsight_api_key.get_secret_value(),
            timeout=settings.hindsight_timeout_seconds,
        )
        # Shared deployment-wide when a store is given (API and worker); evaluation banks stay isolated.
        self._breaker = CircuitBreaker("hindsight", failure_threshold=4, reset_after=30, store=breaker_store)

    async def close(self) -> None:
        await self._client.aclose()  # type: ignore[no-untyped-call]  # untyped third-party method

    async def _call(self, operation: Any, *, attempts: int = 2, attempt_timeout: float | None = None) -> Any:
        return await call_with_resilience(
            operation,
            breaker=self._breaker,
            attempt_timeout=attempt_timeout or self._settings.hindsight_timeout_seconds,
            attempts=attempts,
            retry_on=_is_retryable,
        )

    # ------------------------------------------------------------------ health
    async def ping(self) -> str:
        version = await self._call(self._client.aget_version, attempts=1, attempt_timeout=5)
        return str(version.api_version)

    # ------------------------------------------------------------------ reasoning
    async def reflect(
        self,
        *,
        query: str,
        context: str,
        response_schema: dict[str, Any],
        tags: tuple[str, ...] | list[str] = REFLECT_SCOPE_TAGS,
        budget: Budget = "mid",
    ) -> ReflectResult:
        async def _run() -> Any:
            return await self._client.areflect(
                bank_id=self.bank_id,
                query=query,
                context=context,
                budget=budget,
                response_schema=response_schema,
                include_facts=True,
                include_tool_calls=self._settings.hindsight_include_tool_calls,
                tags=list(tags),
                # "any" keeps untagged consolidated observations in scope alongside tagged documents.
                tags_match="any",
            )

        timeout = (
            self._settings.hindsight_reflect_high_timeout_seconds
            if budget == "high"
            else self._settings.hindsight_reflect_timeout_seconds
        )
        response = await self._call(_run, attempts=1, attempt_timeout=timeout)
        based_on = response.based_on
        memories = (based_on.memories or []) if based_on else []
        mental_models = (getattr(based_on, "mental_models", None) or []) if based_on else []
        usage = response.usage
        return ReflectResult(
            structured=response.structured_output,
            structured_error=response.structured_output_error,
            text=response.text or "",
            facts=[_to_fact(memory) for memory in memories],
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
            mental_models=[_to_mental_model(model) for model in mental_models],
        )

    # ------------------------------------------------------------------ writes
    async def submit_retain(
        self,
        *,
        document_id: str,
        item: dict[str, Any],
        operation_id: str,
    ) -> str | None:
        """Submit an asynchronous retain. Re-submitting the same operation_id is idempotent."""

        async def _run() -> Any:
            return await self._client.aretain_batch(
                bank_id=self.bank_id,
                items=[item],
                document_id=document_id,
                retain_async=True,
                operation_id=operation_id,
            )

        response = await self._call(_run, attempts=1)
        return response.operation_id or (response.operation_ids or [None])[0] or operation_id

    async def submit_batch(self, items: list[dict[str, Any]]) -> list[str]:
        """Asynchronous bulk retain (disposable evaluation and probe banks). Returns the operation IDs.

        Uses the same async path as the memory worker. Synchronous retain is avoided on purpose: it
        exercises a different server path and has failed on Hindsight's side while async kept working.
        Re-sending is safe because every item carries a stable document ID and replaces itself.
        """

        async def _run() -> Any:
            return await self._client.aretain_batch(bank_id=self.bank_id, items=items, retain_async=True)

        response = await self._call(_run, attempts=3)
        return [op for op in (response.operation_ids or [response.operation_id]) if op]

    async def operation_status(self, operation_id: str) -> OperationStatus:
        async def _run() -> Any:
            return await self._client.operations.get_operation_status(self.bank_id, operation_id)

        response = await self._call(_run)
        metadata = response.result_metadata or {}
        # Only the extracted-unit count is meaningful. Zero is a real result (nothing searchable was
        # extracted) and must be preserved; `items_count` is the number of submitted items, not memories.
        count = metadata.get("memory_unit_count")
        return OperationStatus(
            operation_id=response.operation_id,
            status=response.status,
            error_message=response.error_message,
            memory_unit_count=int(count) if count is not None else None,
        )

    async def bank_stats(self, *, refresh: bool = False) -> BankStats:
        """Also proves the bank exists: a missing bank is an error, not an empty answer.

        `refresh` forces Hindsight to recompute; health checks use the cached value.
        """

        async def _run() -> Any:
            return await self._client.banks.get_agent_stats(self.bank_id, refresh=refresh)

        stats = await self._call(_run, attempts=1, attempt_timeout=10)
        return BankStats(
            documents=stats.total_documents,
            observations=stats.total_observations or 0,
            pending_operations=stats.pending_operations,
            pending_consolidation=stats.pending_consolidation or 0,
            failed_consolidation=stats.failed_consolidation or 0,
        )

    async def list_documents(self) -> list[StoredDocument]:
        documents: list[StoredDocument] = []
        while True:
            offset = len(documents)

            async def _page(offset: int = offset) -> Any:
                return await self._client.documents.list_documents(self.bank_id, limit=500, offset=offset)

            page = await self._call(_page, attempts=2, attempt_timeout=30)
            documents += [
                StoredDocument(item.id, list(item.tags or []), item.memory_unit_count or 0)
                for item in page.items
            ]
            if not page.items or len(documents) >= page.total:
                return documents

    async def delete_document(self, document_id: str) -> None:
        await self._call(
            lambda: self._client.documents.delete_document(self.bank_id, document_id), attempts=1
        )

    async def directive_names(self) -> list[str]:
        existing = await self._call(lambda: self._client.alist_directives(bank_id=self.bank_id, limit=100))
        return [directive.name for directive in existing.items if directive.is_active]

    async def webhook_urls(self) -> list[str]:
        existing = await self._call(lambda: self._client.webhooks.list_webhooks(self.bank_id))
        return [webhook.url for webhook in existing.items if set(WEBHOOK_EVENTS) <= set(webhook.event_types)]

    async def delete_bank(self) -> None:
        await self._client.banks.delete_bank(self.bank_id)

    # ------------------------------------------------------------------ configuration
    async def apply_bank_config(
        self, config: BankConfig, *, memory_defense: dict[str, Any] | None
    ) -> list[str]:
        """Create the bank if needed and converge its configuration. Safe to run repeatedly.

        Returns the Memory Defense detectors actually active. The caller decides what is required.
        """
        await self._client.acreate_bank(
            bank_id=config.bank_id,
            background=config.background,
            retain_mission=config.retain_mission,
            enable_observations=True,
            observations_mission=config.observations_mission,
            reflect_mission=config.reflect_mission,
        )
        await self._client.aupdate_bank_config(
            config.bank_id,
            retain_mission=config.retain_mission,
            retain_extraction_mode="verbose",
            observations_mission=config.observations_mission,
            reflect_mission=config.reflect_mission,
            enable_observations=True,
            entity_labels=[label.to_api() for label in config.entity_labels],
            entities_allow_free_form=True,
            disposition_skepticism=DISPOSITION["skepticism"],
            disposition_literalism=DISPOSITION["literalism"],
            disposition_empathy=DISPOSITION["empathy"],
            audit_log_enabled=True,
        )
        detectors = await self._apply_memory_defense(config.bank_id, memory_defense) if memory_defense else []
        await self._sync_directives(config)
        return detectors

    async def _apply_memory_defense(self, bank_id: str, policy: dict[str, Any]) -> list[str]:
        """Apply the policy, keeping only the detectors this Hindsight plan is entitled to.

        400 = some detector is not on the plan (`detectors_not_entitled`), 402/403 = no Memory Defense
        at all. 422 = our policy is malformed, which is a bug and is raised.
        """
        rules: list[dict[str, str]] = list(policy.get("rules", []))
        try:
            await self._client.aupdate_bank_config(bank_id, memory_defense=policy)
            return [rule["on"] for rule in rules]
        except (ApiException, aiohttp.ClientResponseError) as error:
            if error.status in PLAN_LIMIT_STATUSES:
                log.warning("memory_defense_unavailable", status=error.status)
                return []
            if error.status != 400 or len(rules) < 2:
                raise
        entitled = []
        for rule in rules:
            try:
                await self._client.aupdate_bank_config(bank_id, memory_defense={**policy, "rules": [rule]})
                entitled.append(rule)
            except (ApiException, aiohttp.ClientResponseError) as error:
                if error.status not in (400, *PLAN_LIMIT_STATUSES):
                    raise
                log.warning("memory_defense_detector_not_entitled", detector=rule["on"], status=error.status)
        # Each attempt replaced the policy, so finish with exactly the entitled rules (or switch it off).
        await self._client.aupdate_bank_config(
            bank_id, memory_defense={**policy, "rules": entitled, "enabled": bool(entitled)}
        )
        return [rule["on"] for rule in entitled]

    async def bank_config(self) -> dict[str, Any]:
        """The fully resolved configuration Hindsight is actually using (read back, not what we sent)."""
        response: dict[str, Any] = await self._call(
            lambda: self._client.aget_bank_config(self.bank_id), attempts=1, attempt_timeout=10
        )
        return response

    async def _sync_directives(self, config: BankConfig) -> None:
        existing = await self._client.alist_directives(bank_id=config.bank_id, limit=100)
        by_name = {directive.name: directive for directive in existing.items}
        for priority, (name, content) in enumerate(reversed(config.directives), start=1):
            current = by_name.get(name)
            if current and current.content == content and current.is_active:
                continue
            if current:
                await self._client.directives.delete_directive(config.bank_id, current.id)
            await self._client.acreate_directive(
                bank_id=config.bank_id, name=name, content=content, priority=priority, is_active=True
            )

    async def ensure_webhook(self, *, url: str, secret: str) -> str:
        existing = await self._client.webhooks.list_webhooks(self.bank_id)
        for webhook in existing.items:
            if webhook.url == url and set(WEBHOOK_EVENTS) <= set(webhook.event_types):
                return webhook.id
        created = await self._client.webhooks.create_webhook(
            self.bank_id,
            CreateWebhookRequest(url=url, secret=secret, event_types=WEBHOOK_EVENTS, enabled=True),
        )
        return created.id
