"""Secret and PII redaction.

Applied to engineer input, uploaded logs, everything sent to Hindsight and every
log line. Patterns favour recall over precision: a redacted false positive is
cheap, a leaked credential is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("razorpay_key", re.compile(r"\brzp_(?:live|test)_[A-Za-z0-9]{10,}\b")),
    ("groq_key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("connection_string", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@]+@[^\s]+", re.IGNORECASE)),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret"
            r"|private[_-]?key)\b(\s*[:=]\s*|\"\s*:\s*\")(?!\[REDACTED)([^\s\"',;]+)"
        ),
    ),
)

_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("card_number", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("phone", re.compile(r"(?<![\w-])(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){9,11}\d(?![\w-])")),
    ("aadhaar", re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b")),
    ("pan", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
)


@dataclass(frozen=True)
class Redaction:
    kind: str
    category: str  # "secret" | "pii"


@dataclass
class RedactionResult:
    text: str
    redactions: list[Redaction] = field(default_factory=list)

    @property
    def secret_count(self) -> int:
        return sum(1 for r in self.redactions if r.category == "secret")

    @property
    def pii_count(self) -> int:
        return sum(1 for r in self.redactions if r.category == "pii")

    @property
    def changed(self) -> bool:
        return bool(self.redactions)


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def redact_text(text: str, *, include_pii: bool = True) -> RedactionResult:
    result = RedactionResult(text=text)

    for kind, pattern in _SECRET_PATTERNS:

        def _replace_secret(match: re.Match[str], kind: str = kind) -> str:
            result.redactions.append(Redaction(kind, "secret"))
            if kind == "credential_assignment":
                return f"{match.group(1)}{match.group(2)}[REDACTED_SECRET]"
            return "[REDACTED_SECRET]"

        result.text = pattern.sub(_replace_secret, result.text)

    if not include_pii:
        return result

    for kind, pattern in _PII_PATTERNS:

        def _replace_pii(match: re.Match[str], kind: str = kind) -> str:
            value = match.group(0)
            if kind == "card_number":
                digits = re.sub(r"\D", "", value)
                if not (13 <= len(digits) <= 19 and _luhn_valid(digits)):
                    return value
            result.redactions.append(Redaction(kind, "pii"))
            return f"[REDACTED_{kind.upper()}]"

        result.text = pattern.sub(_replace_pii, result.text)

    return result
