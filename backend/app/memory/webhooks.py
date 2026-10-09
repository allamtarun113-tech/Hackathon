"""Hindsight webhook signature verification.

Only the timestamped header is accepted:

    X-Hindsight-Signature-V2: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">

The legacy `X-Hindsight-Signature: sha256=<hex>` header signs the body alone, so a captured
delivery could be replayed indefinitely; it is deliberately rejected. Deliveries outside the
tolerance window are rejected as replays.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

SIGNATURE_HEADER = "x-hindsight-signature-v2"


def _parse(header: str) -> tuple[int, list[str]] | None:
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1" and value:
            signatures.append(value)
    if timestamp is None or not signatures:
        return None
    return timestamp, signatures


def verify_signature(
    *,
    body: bytes,
    headers: Mapping[str, str],
    secret: str,
    tolerance_seconds: int,
    now: float | None = None,
) -> bool:
    if not secret:
        return False
    header = next((value for key, value in headers.items() if key.lower() == SIGNATURE_HEADER), None)
    parsed = _parse(header) if header else None
    if parsed is None:
        return False

    timestamp, signatures = parsed
    current = now if now is not None else time.time()
    if abs(current - timestamp) > tolerance_seconds:
        return False

    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)
