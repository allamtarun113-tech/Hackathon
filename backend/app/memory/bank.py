"""Declarative configuration for the shared `pagermind-incidents` Hindsight bank.

Everything that shapes how Hindsight extracts, consolidates and reasons lives here
so it can be reviewed like code and re-applied idempotently by `pagermind-bank apply`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RETAIN_MISSION = (
    "Extract the affected service, environment, customer impact, exact error signatures (verbatim, as their own "
    "entities), alerts, relevant "
    "deployments, actions attempted, outcome of each action, root cause, final resolution, commands used, "
    "engineer attribution and follow-up work. Ignore greetings, speculation without evidence, repeated status "
    "updates and unrelated conversation."
)

OBSERVATIONS_MISSION = (
    "Identify durable incident-response patterns supported by multiple incidents. Track recurring error "
    "signatures, successful fixes, consistently unsuccessful actions, service dependencies, superseded fixes and "
    "contradictions. Do not generalize from a single unverified feedback event."
)

REFLECT_MISSION = (
    "Act as a careful senior on-call engineer. Match incidents primarily through error signatures and confirmed "
    "causes. Cite only retrieved incident IDs. Distinguish recent facts from superseded guidance. Recommend only "
    "actions supported by evidence or clearly label them as diagnostic hypotheses. Say that the problem appears "
    "new when the evidence is weak."
)

BACKGROUND = (
    "Kartly is an Indian online shopping and payments platform with about two million daily active users. Peak "
    "traffic is 19:00-23:00 IST and during the 'Midnight Deals' campaign (00:00-03:00 IST). This bank is the "
    "shared long-term memory of Kartly's on-call engineers."
)

# Shared by the production bank and every evaluation bank. Each rule names a detector with `on` and an
# action (redact | block); the API rejects anything else with 422.
# Schema: https://hindsight.vectorize.io/developer/memory-defense
MEMORY_DEFENSE_RULES: tuple[dict[str, str], ...] = (
    {"on": "sensitive_data", "action": "redact"},
    # Not on every plan (`detectors_not_entitled`); PagerMind's own injection guard screens content anyway.
    {"on": "prompt_injection", "action": "block"},
)
MEMORY_DEFENSE: dict[str, Any] = {"enabled": True, "rules": list(MEMORY_DEFENSE_RULES)}

DISPOSITION = {"skepticism": 4, "literalism": 4, "empathy": 2}

DIRECTIVES: tuple[tuple[str, str], ...] = (
    (
        "no-invented-ids",
        "Never invent incident IDs. Cite only incident IDs that appear in retrieved memories.",
    ),
    ("no-execution-claims", "Never claim that an action or command was executed. You only suggest actions."),
    ("correlation-not-causation", "Never treat correlation as confirmed causation."),
    ("no-secrets", "Never repeat secrets, tokens, passwords or customer contact details found in logs."),
    (
        "no-evaluation-data",
        "Never use or reveal held-out evaluation data, expected answers or grading labels.",
    ),
    (
        "destructive-warning",
        "Never recommend destructive commands (delete, drop, truncate, force, flush) without an explicit warning "
        "and a note that a human must approve them.",
    ),
    ("prefer-newest", "Prefer the newest confirmed resolution when guidance conflicts."),
    (
        "abstain-when-weak",
        "Abstain and say the problem appears new when historical evidence is insufficient.",
    ),
)


@dataclass(frozen=True)
class EntityLabel:
    key: str
    values: tuple[str, ...] = ()
    free_text: bool = False
    as_tag: bool = True
    # Hindsight label types: "value"/"text" hold one value per fact, "multi-values"/"multi-text" any number.
    multiple: bool = False

    def to_api(self) -> dict[str, Any]:
        if self.free_text:
            return {"key": self.key, "type": "multi-text" if self.multiple else "text", "tag": self.as_tag}
        values = [{"value": value} for value in self.values]
        kind = "multi-values" if self.multiple else "value"
        return {"key": self.key, "type": kind, "values": values, "tag": self.as_tag}


@dataclass(frozen=True)
class BankConfig:
    bank_id: str
    retain_mission: str = RETAIN_MISSION
    observations_mission: str = OBSERVATIONS_MISSION
    reflect_mission: str = REFLECT_MISSION
    background: str = BACKGROUND
    directives: tuple[tuple[str, str], ...] = DIRECTIVES
    entity_labels: tuple[EntityLabel, ...] = field(default_factory=tuple)


def entity_labels(services: list[str], teams: list[str]) -> tuple[EntityLabel, ...]:
    """Controlled vocabulary so extraction cannot invent a new spelling of a service or severity."""
    return (
        EntityLabel("service", tuple(sorted(services))),
        EntityLabel("team", tuple(sorted(teams))),
        EntityLabel("environment", ("production", "staging")),
        EntityLabel("severity", ("sev1", "sev2", "sev3", "sev4")),
        EntityLabel("failure_domain", free_text=True),
        # Reflect matches primarily on signatures, so they are first-class entities, not buried in prose.
        # Not a tag: signatures are long free text and tags are exact-match filters.
        # An incident usually has several signatures, so the label must accept more than one per fact.
        EntityLabel("error_signature", free_text=True, as_tag=False, multiple=True),
        EntityLabel("memory_type", ("incident", "procedure", "feedback", "live_step")),
        EntityLabel("action_outcome", ("worked", "partial", "did_not_work", "made_worse")),
        EntityLabel("resolution_status", ("resolved", "mitigated", "open")),
    )


# --------------------------------------------------------------------------- tags
def tag(key: str, value: str) -> str:
    return f"{key}:{value.strip().lower().replace(' ', '-')}"


PRODUCTION_ENVIRONMENT = "production"

# The one tag set every live and evaluation reflect filters on. Every retained memory type (incident,
# feedback, procedure) carries it, so "production memory" is a property of the bank, not of whichever
# document builder happened to remember the tag. Reflect deliberately never filters by service:
# a cross-service signature match is part of the product.
REFLECT_SCOPE_TAGS: tuple[str, ...] = (tag("environment", PRODUCTION_ENVIRONMENT),)


def memory_tags(
    *, memory_type: str, service: str, environment: str = PRODUCTION_ENVIRONMENT, **extra: str | None
) -> list[str]:
    """Base tags shared by every memory document, plus any non-empty extras (`team`, `outcome`, ...)."""
    tags = [tag("environment", environment), tag("service", service), tag("memory_type", memory_type)]
    tags += [tag(key, value) for key, value in extra.items() if value]
    return tags


def incident_tags(
    *, service: str, team: str | None, severity: str, environment: str, source: str, outcome: str = "resolved"
) -> list[str]:
    # Order is part of the document checksum; keep it stable so re-imports do not re-retain every incident.
    tags = [
        tag("environment", environment),
        tag("service", service),
        tag("severity", severity),
        tag("memory_type", "incident"),
        tag("outcome", outcome),
        tag("source", source),
    ]
    if team:
        tags.append(tag("team", team))
    return tags
