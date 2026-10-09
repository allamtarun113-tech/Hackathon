from __future__ import annotations

import pytest

from app.core.redaction import redact_text


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("password=hunter2-very-secret", "hunter2-very-secret"),
        ('{"api_key": "abc123def456ghi789"}', "abc123def456ghi789"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345", "abcdefghijklmnopqrstuvwxyz012345"),
        ("postgres://app:s3cr3tpass@db.internal:5432/main", "s3cr3tpass"),
        ("key AKIAABCDEFGHIJKLMNOP leaked", "AKIAABCDEFGHIJKLMNOP"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3OCJ9.c2lnbmF0dXJlLXZhbHVl", "eyJhbGciOiJIUzI1NiJ9"),
    ],
)
def test_secrets_are_redacted(text: str, secret: str) -> None:
    result = redact_text(text)
    assert secret not in result.text
    assert result.secret_count >= 1


def test_contact_details_are_redacted() -> None:
    result = redact_text("Customer jane.doe@example.com called from +91-98765-43210 about checkout.")
    assert "jane.doe@example.com" not in result.text
    assert "98765-43210" not in result.text
    assert result.pii_count == 2


def test_valid_card_numbers_are_redacted() -> None:
    result = redact_text("card 4111 1111 1111 1111")
    assert "4111 1111 1111 1111" not in result.text
    assert any(r.kind == "card_number" for r in result.redactions)


def test_operational_text_is_untouched() -> None:
    line = "HikariPool-1 - Connection is not available, request timed out after 30000ms."
    result = redact_text(line)
    assert result.text == line
    assert not result.changed


def test_redaction_is_idempotent() -> None:
    once = redact_text(
        "password=hunter2 token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3OCJ9.c2lnbmF0dXJlLXZhbHVl"
    )
    twice = redact_text(once.text)
    assert twice.text == once.text
    assert not twice.changed
