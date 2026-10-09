from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path

from app.core.hashing import payload_checksum, stable_operation_id
from app.guardrails.memory_guard import is_meaningful_feedback, validate_memory_payload
from app.importers.memory import incident_record_from_seed
from app.memory.documents import build_incident_document
from app.memory.webhooks import verify_signature

SECRET = "whsec_test"


def _seed_document(data_dir: Path):  # type: ignore[no-untyped-def]
    incident = json.loads((data_dir / "incidents_history.json").read_text())[0]
    return incident, build_incident_document(incident_record_from_seed(incident, "Payments", changes=[]))


def test_incident_document_is_stable_and_clean(data_dir: Path) -> None:
    incident, document = _seed_document(data_dir)
    assert document.document_id == f"incident:{incident['id']}"
    assert document.update_mode == "replace"
    assert "pattern_id" not in json.dumps(document.payload())
    assert payload_checksum(document.payload()) == payload_checksum(_seed_document(data_dir)[1].payload())


def test_memory_guard_accepts_seed_documents(data_dir: Path) -> None:
    _, document = _seed_document(data_dir)
    payload = document.payload()
    assert validate_memory_payload(payload, expected_checksum=payload_checksum(payload)).allowed


def test_memory_guard_rejects_evaluation_material(data_dir: Path) -> None:
    _, document = _seed_document(data_dir)
    payload = document.payload()
    payload["item"]["content"] += "\nexpected_incident_ids: INC-2001 (EVAL-001)"
    verdict = validate_memory_payload(payload, expected_checksum=payload_checksum(payload))
    assert not verdict.allowed and verdict.reason == "evaluation_data_detected"


def test_memory_guard_rejects_tampered_payload(data_dir: Path) -> None:
    _, document = _seed_document(data_dir)
    payload = document.payload()
    checksum = payload_checksum(payload)
    payload["item"]["content"] = "tampered"
    assert validate_memory_payload(payload, expected_checksum=checksum).reason == "checksum_mismatch"


def test_bare_ratings_are_not_retained() -> None:
    assert not is_meaningful_feedback(outcome=None, note=None, confirmed=False).allowed
    assert not is_meaningful_feedback(outcome="worked", note="ok", confirmed=False).allowed
    assert is_meaningful_feedback(
        outcome="did_not_work", note="Restarting pods did not reduce the error rate.", confirmed=False
    ).allowed
    assert is_meaningful_feedback(outcome="worked", note=None, confirmed=True).allowed


def test_operation_ids_are_deterministic() -> None:
    assert stable_operation_id("bank", "incident:INC-1", "abc") == stable_operation_id(
        "bank", "incident:INC-1", "abc"
    )
    assert stable_operation_id("bank", "incident:INC-1", "abc") != stable_operation_id(
        "bank", "incident:INC-1", "abd"
    )


def test_webhook_signature_v2() -> None:
    body = b'{"event":"retain.completed"}'
    timestamp = int(datetime.now().timestamp())
    signature = hmac.new(SECRET.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    headers = {"X-Hindsight-Signature-V2": f"t={timestamp},v1={signature}"}
    assert verify_signature(body=body, headers=headers, secret=SECRET, tolerance_seconds=300)
    assert not verify_signature(body=body + b" ", headers=headers, secret=SECRET, tolerance_seconds=300)
    assert not verify_signature(
        body=body, headers=headers, secret=SECRET, tolerance_seconds=300, now=timestamp + 3600
    )


def test_legacy_and_unsigned_webhooks_are_rejected() -> None:
    body = b"{}"
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    legacy = {"X-Hindsight-Signature": f"sha256={signature}"}
    assert not verify_signature(body=body, headers=legacy, secret=SECRET, tolerance_seconds=300)
    assert not verify_signature(body=body, headers={}, secret=SECRET, tolerance_seconds=300)

    timestamp = int(datetime.now().timestamp())
    valid = hmac.new(SECRET.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    headers = {"X-Hindsight-Signature-V2": f"t={timestamp},v1={valid}"}
    assert not verify_signature(body=body, headers=headers, secret="", tolerance_seconds=300)


def test_old_signed_delivery_cannot_be_replayed() -> None:
    body = b'{"event":"retain.completed","operation_id":"op-1"}'
    signed_at = int(datetime.now().timestamp()) - 600
    signature = hmac.new(SECRET.encode(), f"{signed_at}.".encode() + body, hashlib.sha256).hexdigest()
    headers = {"X-Hindsight-Signature-V2": f"t={signed_at},v1={signature}"}
    assert not verify_signature(body=body, headers=headers, secret=SECRET, tolerance_seconds=300)
