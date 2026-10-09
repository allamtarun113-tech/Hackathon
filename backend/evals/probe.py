"""Live probe: verify against real Hindsight what offline tests can only assume.

Creates a small disposable bank with production's configuration, retains a handful of real
incident documents, waits for consolidation, runs the production reflect path, reports what
Hindsight actually returned, and deletes the bank. Cheap enough to run before every demo.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.agent import grounding
from app.core.config import Settings
from app.core.logging import get_logger
from app.importers.source import SeedSource
from app.memory.bank import MEMORY_DEFENSE, REFLECT_SCOPE_TAGS, BankConfig, entity_labels
from app.memory.client import HindsightGateway
from app.memory.setup import verification_report
from app.memory.signatures import error_signatures
from evals.bank import load_documents, production_documents, wait_for_consolidation
from evals.dataset import load_scenarios

log = get_logger(__name__)

# Generous limits: the probe measures how long reflect really takes, so it must not cut it short.
PROBE_REFLECT_TIMEOUT = 120.0


@dataclass
class ProbeReport:
    bank_id: str
    config: dict[str, bool] = field(default_factory=dict)
    memory_defense_detectors: list[str] = field(default_factory=list)
    documents_retained: int = 0
    observations: int = 0
    failed_consolidation: int = 0
    reflects: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


def _pick_documents(source: SeedSource, scenario_ids: list[str], extra: int) -> list[Any]:
    """The incidents the probe scenarios should match, plus unrelated ones so matching is not trivial."""
    scenarios = {s.input.case_id: s for s in load_scenarios(source.data_dir)}
    wanted = {incident for case in scenario_ids for incident in scenarios[case].labels.expected_incident_ids}
    documents = production_documents(source)
    chosen = [d for d in documents if d.source_id in wanted]
    chosen += [d for d in documents if d.memory_type == "incident" and d.source_id not in wanted][:extra]
    chosen += [d for d in documents if d.memory_type == "procedure"][:1]
    return chosen


async def run_probe(
    settings: Settings, *, scenario_ids: list[str], extra_documents: int = 8, keep_bank: bool = False
) -> ProbeReport:
    bank_id = f"pagermind-probe-{uuid.uuid4().hex[:10]}"
    if bank_id == settings.hindsight_bank_id:
        raise RuntimeError("Refusing to probe the production bank.")
    report = ProbeReport(bank_id=bank_id)
    source = SeedSource(settings.data_dir)
    services = source.catalog["services"]
    teams = {service_id: service["team"] for service_id, service in services.items()}
    probe_settings = settings.model_copy(
        update={
            "hindsight_reflect_timeout_seconds": PROBE_REFLECT_TIMEOUT,
            "hindsight_reflect_high_timeout_seconds": PROBE_REFLECT_TIMEOUT,
        }
    )
    gateway = HindsightGateway(probe_settings, bank_id=bank_id)
    try:
        # 1. Configuration: the API rejects unknown label types, so acceptance itself is a check.
        report.memory_defense_detectors = await gateway.apply_bank_config(
            BankConfig(
                bank_id=bank_id, entity_labels=entity_labels(list(services), sorted(set(teams.values())))
            ),
            memory_defense=MEMORY_DEFENSE,
        )
        report.config = verification_report(await gateway.bank_config())
        report.problems += [f"bank config missing: {name}" for name, ok in report.config.items() if not ok]

        # 2. Retain real documents, entities included, exactly as the worker sends them.
        documents = _pick_documents(source, scenario_ids, extra_documents)
        loaded = await load_documents(gateway, documents, limit_seconds=15 * 60, poll=10)
        report.documents_retained = loaded.documents - len(loaded.empty)
        report.problems += [f"zero facts extracted from {label}" for label in loaded.empty]

        # 3. Observations are built asynchronously after retain.
        await wait_for_consolidation(gateway, limit_seconds=15 * 60, poll=10)
        stats = await gateway.bank_stats(refresh=True)
        report.observations, report.failed_consolidation = stats.observations, stats.failed_consolidation
        if stats.observations == 0:
            report.problems.append("no observations were consolidated")

        # 4. The production reflect path, timed without the production timeout.
        scenarios = {s.input.case_id: s for s in load_scenarios(settings.data_dir)}
        for case_id in scenario_ids:
            scenario = scenarios[case_id]
            started = time.perf_counter()
            result = await grounding.grounded_reflect(
                gateway,
                service_id=scenario.input.service_id,
                severity=scenario.input.severity,
                symptoms=f"{scenario.input.alert_title} ({scenario.input.alert_value})",
                error_signatures=error_signatures(scenario.input.log_excerpt),
                recent_changes=[],
                log_text="\n".join(scenario.input.log_excerpt),
            )
            seconds = round(time.perf_counter() - started, 1)
            facts = grounding.evidence_dicts(result)
            support, surfaced = grounding.incident_support(facts)
            budget = grounding.reflect_budget(scenario.input.severity)
            limit = (
                settings.hindsight_reflect_high_timeout_seconds
                if budget == "high"
                else settings.hindsight_reflect_timeout_seconds
            )
            consolidated = [f for f in facts if f.get("fact_type") in grounding.CONSOLIDATED_TYPES]
            entry = {
                "case_id": case_id,
                "budget": budget,
                "seconds": seconds,
                "production_limit_seconds": limit,
                "within_limit": seconds <= limit,
                "facts": len(facts),
                "fact_types": dict(Counter(str(f.get("fact_type")) for f in facts)),
                "memory_types": dict(Counter(str(f.get("memory_type")) for f in facts)),
                "facts_with_scope_tag": sum(
                    1 for f in facts if set(REFLECT_SCOPE_TAGS) & set(f.get("tags") or [])
                ),
                "facts_with_incident_id": sum(1 for f in facts if f.get("incident_id")),
                "consolidated": len(consolidated),
                "consolidated_naming_incidents": sum(1 for f in consolidated if "INC-" in str(f.get("text"))),
                "surfaced": sorted(surfaced),
                "expected": scenario.labels.expected_incident_ids,
                "expected_surfaced": sorted(set(scenario.labels.expected_incident_ids) & surfaced),
                "support": dict(support),
                "structured_output": result.structured is not None,
            }
            report.reflects.append(entry)
            if not entry["within_limit"]:
                report.problems.append(f"{case_id}: reflect took {seconds}s, production limit is {limit}s")
            if not entry["structured_output"]:
                report.problems.append(f"{case_id}: no structured output")
            if entry["expected"] and not entry["expected_surfaced"]:
                report.problems.append(f"{case_id}: none of the expected incidents were surfaced")
            if entry["facts"] and not entry["facts_with_incident_id"]:
                report.problems.append(f"{case_id}: facts carry no incident_id metadata")
    finally:
        if not keep_bank:
            try:
                await gateway.delete_bank()
            except Exception as error:  # report, but never mask the probe's own result
                report.problems.append(f"could not delete probe bank: {type(error).__name__}")
        await gateway.close()
    return report
