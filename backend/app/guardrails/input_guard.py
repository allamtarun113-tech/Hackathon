"""Input guardrails: length, secrets, PII, prompt injection and topic.

Each check is a small pure function so LangGraph nodes can run them at the
right step, while `guard_input` composes all of them for the CI guardrail suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.redaction import redact_text
from app.guardrails.injection import scan_and_neutralise
from app.guardrails.models import Decision, GuardrailCheck

# Stems of on-call vocabulary. Each stem accepts inflections ("alert" → "alerts", "alerting";
# "crash" → "crashes", "crashing"), so real incident reports are never refused as off-topic.
_ON_CALL_STEMS = (
    "error",
    "exception",
    "fail",
    "outage",
    "incident",
    "alert",
    "alarm",
    "pager",
    "paging",
    "latenc",
    "p9[59]",
    "[45]xx",
    "[45]\\d\\d",
    "timeout",
    "time[sd]? out",
    "timing out",
    "hang",
    "hung",
    "crash",
    "oom",
    "killed",
    "restart",
    "rollback",
    "roll(?:ed|ing)? back",
    "deploy",
    "release",
    "pod",
    "node",
    "container",
    "cluster",
    "kubectl",
    "k8s",
    "kubernetes",
    "cpu",
    "memory",
    "heap",
    "disk",
    "leak",
    "saturat",
    "queue",
    "backlog",
    "backing up",
    "lag",
    "kafka",
    "redis",
    "postgres",
    "database",
    "db",
    "connection",
    "pool",
    "cache",
    "throttl",
    "rate.?limit",
    "sev[1-4]",
    "degrad",
    "slow",
    "spike",
    "stuck",
    "dns",
    "tls",
    "certificate",
    "gateway",
    "upstream",
    "retr(?:y|ies)",
    "logs?\\b",
    "stack ?trace",
    "service",
    "api",
    "endpoint",
    "server",
    "checkout",
    "payment",
    "order",
    "broken",
    "down\\b",
    "unavailable",
    "unreachable",
    "webhook",
    "cron",
    "consumer",
)
_ON_CALL_VOCABULARY = re.compile(r"\b(?:" + "|".join(_ON_CALL_STEMS) + r")", re.IGNORECASE)

OFF_TOPIC_MESSAGE = "PagerMind only helps with production incidents and on-call work."


@dataclass(frozen=True)
class CheckedText:
    text: str
    checks: tuple[GuardrailCheck, ...] = ()
    redaction_count: int = 0


@dataclass
class InputGuardResult:
    text: str
    checks: list[GuardrailCheck] = field(default_factory=list)
    rejected: bool = False
    redaction_count: int = 0

    @property
    def reason_codes(self) -> list[str]:
        return [check.reason_code for check in self.checks if check.reason_code]


# --------------------------------------------------------------------------- individual checks
def enforce_length(text: str, *, max_chars: int) -> CheckedText:
    if len(text) <= max_chars:
        return CheckedText(text)
    return CheckedText(
        text[:max_chars],
        (
            GuardrailCheck(
                "input",
                "length_limit",
                Decision.TRUNCATE_AND_CONTINUE,
                "input_length_exceeded",
                {"original_chars": len(text), "kept_chars": max_chars},
            ),
        ),
    )


def redact_sensitive(text: str) -> CheckedText:
    redaction = redact_text(text)
    checks: list[GuardrailCheck] = []
    if redaction.secret_count:
        checks.append(
            GuardrailCheck(
                "input",
                "secret_detection",
                Decision.REDACT_AND_CONTINUE,
                "secret_detected",
                {"count": redaction.secret_count},
            )
        )
    if redaction.pii_count:
        checks.append(
            GuardrailCheck(
                "input",
                "pii_detection",
                Decision.REDACT_AND_CONTINUE,
                "pii_detected",
                {"count": redaction.pii_count},
            )
        )
    return CheckedText(redaction.text, tuple(checks), len(redaction.redactions))


def neutralise_injection(text: str) -> CheckedText:
    scan = scan_and_neutralise(text)
    if not scan.detected:
        return CheckedText(text)
    reason = {"stacktrace": "instruction_inside_stacktrace", "log": "instruction_inside_log"}.get(
        scan.location or "", "instruction_in_message"
    )
    return CheckedText(
        scan.text,
        (
            GuardrailCheck(
                "input",
                "prompt_injection",
                Decision.SANITIZE_AND_CONTINUE,
                reason,
                {"pattern_count": len(scan.findings), "location": scan.location},
            ),
        ),
    )


def is_on_call_topic(text: str, known_services: set[str] | None = None) -> bool:
    lowered = text.lower()
    if known_services and any(service in lowered for service in known_services):
        return True
    return bool(_ON_CALL_VOCABULARY.search(text))


def check_topic(text: str, known_services: set[str] | None = None) -> GuardrailCheck | None:
    if is_on_call_topic(text, known_services):
        return None
    return GuardrailCheck("input", "topic_check", Decision.REJECT, "outside_oncall_scope")


# --------------------------------------------------------------------------- composed pipeline
def guard_input(text: str, *, max_chars: int, known_services: set[str] | None = None) -> InputGuardResult:
    result = InputGuardResult(text=text)
    for step in (
        lambda value: enforce_length(value, max_chars=max_chars),
        redact_sensitive,
        neutralise_injection,
    ):
        checked = step(result.text)
        result.text = checked.text
        result.checks.extend(checked.checks)
        result.redaction_count += checked.redaction_count

    topic = check_topic(result.text, known_services)
    if topic:
        result.checks.append(topic)
        result.rejected = True

    if not result.checks:
        result.checks.append(GuardrailCheck("input", "input_guard", Decision.ALLOW))
    return result
