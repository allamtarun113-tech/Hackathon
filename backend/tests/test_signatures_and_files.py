from __future__ import annotations

from app.api.ratelimit import MemoryRateLimiter, client_ip
from app.guardrails.file_guard import validate_upload
from app.memory.signatures import error_signatures

MAX = 1024 * 1024


def test_signatures_strip_volatile_tokens() -> None:
    lines = [
        "2026-09-29 02:40:36.698  WARN 1 --- [http-nio-8080-exec-132] com.zaxxer.hikari.pool.HikariPool       : "
        "HikariPool-1 - Connection is not available, request timed out after 30000ms.",
        "2026-09-29 02:41:01.100  WARN 1 --- [http-nio-8080-exec-7] com.zaxxer.hikari.pool.HikariPool       : "
        "HikariPool-1 - Connection is not available, request timed out after 30000ms.",
        "2026-09-29 02:41:02.000  INFO 1 --- [main] c.k.Health : all good",
    ]
    assert error_signatures(lines) == [
        "HikariPool-1 - Connection is not available, request timed out after 30000ms."
    ]


def test_file_guard_accepts_text_logs() -> None:
    check = validate_upload("app.log", b"ERROR something failed\n", max_bytes=MAX)
    assert check.accepted and check.mime_type == "text/plain"


def test_file_guard_rejects_binaries_and_bad_json() -> None:
    assert not validate_upload("app.log", b"\x7fELF\x02\x01", max_bytes=MAX).accepted
    assert not validate_upload("evil.exe", b"MZ", max_bytes=MAX).accepted
    assert validate_upload("data.json", b"{broken", max_bytes=MAX).check.reason_code == "corrupted_json"
    assert validate_upload("big.log", b"x" * 20, max_bytes=10).check.reason_code == "file_too_large"


def test_rate_limiter_blocks_after_limit() -> None:
    limiter = MemoryRateLimiter(limit=2, window_seconds=60)
    assert limiter.allow("1.2.3.4")[0]
    assert limiter.allow("1.2.3.4")[0]
    allowed, retry_after = limiter.allow("1.2.3.4")
    assert not allowed and retry_after > 0
    assert limiter.allow("5.6.7.8")[0]


def test_client_ip_ignores_spoofed_forwarded_for() -> None:
    # The client wrote "1.1.1.1" itself; Render (one trusted hop) appended the real address.
    assert client_ip("1.1.1.1, 203.0.113.9", "10.0.0.2", trusted_hops=1) == "203.0.113.9"
    assert client_ip("1.1.1.1", "10.0.0.2", trusted_hops=0) == "10.0.0.2"
    assert client_ip(None, "10.0.0.2", trusted_hops=1) == "10.0.0.2"
    assert client_ip("203.0.113.9", "10.0.0.2", trusted_hops=2) == "10.0.0.2"
