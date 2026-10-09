"""Extract distinct error signatures from raw log lines.

Timestamps, request IDs, order IDs and pod hashes are stripped so the same
failure produces the same signature across incidents. Healthy lines are skipped:
'Pool stats ... waiting=0' would otherwise pull in connection-pool incidents even
though it proves the pool is fine.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_TIMESTAMPS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?Z?\s*",
        r"^\d+:M \d{2} \w{3} \d{4} [\d:.]+\s*",
        r"^ts=\S+\s*",
        r"^\S+ - - \[[^\]]+\]\s*",
    )
)
_HEALTHY_MARKERS = (" INFO ", '"level":"info"', "level=info", "  Normal ", " DEBUG ")
_VOLATILE = re.compile(
    r"(ORD|SKU)-\d+|traceId=\w+|u_\d+|exec-\d+|consumer-\d+|worker-\d+|latencyMs=\d+|\"latency_ms\":\d+,?"
    r"|(?<=\s)\d{2,4}ms(?=\s|$)|event_loop_lag_ms=\d+|processed \d+ rows|offset \d+|upstream_response_time=[\d.]+"
    r"|cart=\d+|msg-\d+|\"attempt\":\d+,?|(?<=pod/)[\w-]+?-[a-z0-9]{10}-[a-z0-9]{5}|\"(ts|EdgeStartTimestamp)\":\"[^\"]*\",?"
    r"|req-\d+|\"request_id\":\"[^\"]*\",?"
)
_JAVA_PREFIX = re.compile(r"^(ERROR|WARN|DEBUG|INFO)\s+\d+ --- \[[^\]]*\]\s+\S+\s+:\s*")
_NODE_PREFIX = re.compile(r"^(ERROR|WARN|INFO)\s+\[[\w-]+\]\s*")
_JSON_FIELDS = re.compile(
    r"\"(msg|error|SecurityAction|SecurityRuleDescription|ClientRequestPath|EdgeResponseStatus)\":\"?([^\",}]*)"
)


def _is_noise(line: str) -> bool:
    exhausted_pool = "Pool stats" in line and "waiting=0)" not in line
    if exhausted_pool:
        return False
    return "Pool stats" in line or any(marker in line for marker in _HEALTHY_MARKERS)


def _normalise(line: str) -> str:
    text = line.strip()
    for pattern in _TIMESTAMPS:
        text = pattern.sub("", text)
    return re.sub(r"\s{2,}", " ", _VOLATILE.sub("", text)).strip()


def _message_only(line: str) -> str:
    if line.startswith("{"):
        return " ".join(
            value if key in ("msg", "error") else f"{key}={value}"
            for key, value in _JSON_FIELDS.findall(line)
        )
    return _NODE_PREFIX.sub("", _JAVA_PREFIX.sub("", line))


def error_signatures(lines: Iterable[str], limit: int = 5) -> list[str]:
    """Distinct, normalised warning/error lines in order of first appearance."""
    signatures: list[str] = []
    for line in lines:
        if _is_noise(line):
            continue
        signature = _message_only(_normalise(line)).strip()
        if signature and signature not in signatures:
            signatures.append(signature)
        if len(signatures) >= limit:
            break
    return signatures
