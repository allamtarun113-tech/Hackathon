"""Disposable evaluation bank loaded with the same retain documents as production, minus held-out cases.

Production retains every memory-split incident (with its reviewed change links) and every approved
runbook. The evaluation bank applies the same bank configuration (including Memory Defense) and builds
its documents with the same builders from the same seed inputs. The only difference: a runbook's
approval timestamp is fixed here, where production uses the time it was imported into Supabase.

Scoring starts only after Hindsight has finished consolidating observations, otherwise an evaluation
would under-measure exactly the cross-incident knowledge production benefits from.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redaction import redact_text
from app.importers.files import runbook_title
from app.importers.memory import incident_record_from_seed
from app.importers.source import SeedSource
from app.memory.bank import MEMORY_DEFENSE, BankConfig, entity_labels
from app.memory.client import HindsightGateway
from app.memory.documents import (
    ChangeEvidence,
    MemoryDocument,
    build_incident_document,
    build_runbook_document,
)

log = get_logger(__name__)
BATCH_SIZE = 25
CONSOLIDATION_POLL_SECONDS = 15
CONSOLIDATION_TIMEOUT_SECONDS = 45 * 60
_MEMORY_RELATIONSHIPS = {"causal", "contributing", "ruled_out"}
# Seed runbooks have no approval timestamp in the files; a fixed one keeps documents reproducible.
_RUNBOOK_APPROVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class ConsolidationIncomplete(RuntimeError):
    """Hindsight did not finish consolidating observations in time."""


async def wait_for_consolidation(
    gateway: HindsightGateway,
    *,
    limit_seconds: float = CONSOLIDATION_TIMEOUT_SECONDS,
    poll: float = CONSOLIDATION_POLL_SECONDS,
) -> None:
    deadline = time.monotonic() + limit_seconds
    while True:
        stats = await gateway.bank_stats(refresh=True)
        log.info(
            "evaluation_bank_consolidating",
            bank_id=gateway.bank_id,
            pending_operations=stats.pending_operations,
            pending_consolidation=stats.pending_consolidation,
            observations=stats.observations,
        )
        if stats.pending_operations == 0 and stats.pending_consolidation == 0:
            if stats.failed_consolidation:
                log.warning("evaluation_bank_consolidation_failures", failed=stats.failed_consolidation)
            return
        if time.monotonic() >= deadline:
            raise ConsolidationIncomplete(
                f"{stats.pending_consolidation} memories still pending consolidation after {limit_seconds:.0f}s"
            )
        await asyncio.sleep(poll)


class RetainFailed(RuntimeError):
    """Hindsight could not retain some evaluation documents, so the bank would not match production."""


@dataclass
class LoadResult:
    documents: int
    operations: int
    empty: list[str] = field(default_factory=list)


async def load_documents(
    gateway: HindsightGateway,
    documents: list[MemoryDocument],
    *,
    limit_seconds: float = CONSOLIDATION_TIMEOUT_SECONDS,
    poll: float = CONSOLIDATION_POLL_SECONDS,
) -> LoadResult:
    """Retain the documents through the async path the memory worker uses, and wait until each is done.

    A failed operation aborts the load; a document from which Hindsight extracted zero facts is reported
    (production sends those to `needs_review`, so the evaluation must not silently count them as memory).
    """
    operations: dict[str, str] = {}
    for batch in _chunks(documents, BATCH_SIZE):
        ids = await gateway.submit_batch([{**d.item, "document_id": d.document_id} for d in batch])
        # One operation per item when Hindsight returns several, otherwise one for the whole batch.
        labels = [d.document_id for d in batch] if len(ids) == len(batch) else [f"batch of {len(batch)}"]
        operations.update(zip(ids, labels, strict=False))
        log.info("evaluation_bank_batch_submitted", bank_id=gateway.bank_id, documents=len(batch))

    deadline = time.monotonic() + limit_seconds
    pending = dict(operations)
    result = LoadResult(documents=len(documents), operations=len(operations))
    while pending:
        for operation_id, label in list(pending.items()):
            status = await gateway.operation_status(operation_id)
            if status.status == "completed":
                pending.pop(operation_id)
                if status.memory_unit_count == 0:
                    result.empty.append(label)
            elif status.status in ("failed", "cancelled"):
                raise RetainFailed(f"{label}: {status.status} ({status.error_message or 'no detail'})")
        if not pending:
            break
        if time.monotonic() >= deadline:
            raise RetainFailed(f"{len(pending)} retain operations still running after {limit_seconds:.0f}s")
        log.info("evaluation_bank_retaining", bank_id=gateway.bank_id, pending=len(pending))
        await asyncio.sleep(poll)
    if result.empty:
        log.warning("evaluation_bank_empty_documents", count=len(result.empty), sample=result.empty[:5])
    return result


class HeldOutLeak(RuntimeError):
    """A held-out evaluation case would have been written into the evaluation bank."""


def _chunks[T](items: list[T], size: int) -> Iterator[list[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _reviewed_changes(source: SeedSource) -> dict[str, list[ChangeEvidence]]:
    """Mirror of `deployments.reviewed_links_for_memory`, computed from the seed files."""
    deploys = {deploy["id"]: deploy for deploy in source.deployments}
    links = sorted(
        (
            link
            for link in source.production("incident_change_links")
            if link.get("reviewed_at")
            and link["relationship"] in _MEMORY_RELATIONSHIPS
            and link["change_id"] in deploys
        ),
        key=lambda link: -float(link.get("confidence") or 0),
    )
    by_incident: dict[str, list[ChangeEvidence]] = {}
    for link in links:
        deploy = deploys[link["change_id"]]
        by_incident.setdefault(link["incident_id"], []).append(
            ChangeEvidence(
                change_id=link["change_id"],
                service_id=deploy["service"],
                relationship=link["relationship"],
                summary=deploy.get("change_summary") or link.get("evidence") or "",
            )
        )
    return by_incident


def production_documents(source: SeedSource) -> list[MemoryDocument]:
    """Every document production retains from the seed data: incidents and approved runbooks."""
    services = source.catalog["services"]
    teams = {service_id: service["team"] for service_id, service in services.items()}
    changes = _reviewed_changes(source)
    documents = [
        build_incident_document(
            incident_record_from_seed(
                incident, teams.get(incident["service"]), changes.get(incident["id"], [])
            )
        )
        for incident in source.incidents
    ]
    for path in source.runbook_paths():
        markdown = redact_text(path.read_text(encoding="utf-8"), include_pii=False).text
        documents.append(
            build_runbook_document(
                runbook_id=f"RB-{path.stem}",
                service_id=path.stem if path.stem in services else None,
                title=runbook_title(markdown, path.stem),
                version=1,
                markdown=markdown,
                approved_at=_RUNBOOK_APPROVED_AT,
            )
        )
    held_out = {scenario["id"] for scenario in source.scenarios}
    leaked = sorted(held_out & {document.source_id for document in documents})
    if leaked:
        raise HeldOutLeak(f"Held-out cases would enter the evaluation bank: {', '.join(leaked[:5])}")
    return documents


async def build_evaluation_bank(settings: Settings, bank_id: str) -> tuple[HindsightGateway, set[str]]:
    """Create and populate the bank. Returns the gateway and the set of valid incident IDs."""
    source = SeedSource(settings.data_dir)
    services = source.catalog["services"]
    teams = sorted({service["team"] for service in services.values()})

    gateway = HindsightGateway(settings, bank_id=bank_id)
    await gateway.apply_bank_config(
        BankConfig(bank_id=bank_id, entity_labels=entity_labels(list(services), teams)),
        memory_defense=MEMORY_DEFENSE,
    )

    # Fully indexed and consolidated before the first scenario runs.
    await load_documents(gateway, production_documents(source))
    await wait_for_consolidation(gateway)
    return gateway, {incident["id"] for incident in source.incidents}
