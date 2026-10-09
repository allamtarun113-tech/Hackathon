"""The demo journey end to end, plus SSE failure paths.

upload log -> memory-grounded recommendation with citations -> baseline comparison ->
provisional feedback -> operator resolution verifies it -> outbox -> worker -> webhook ->
retained -> a related incident can cite the newly remembered incident.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import DependencyUnavailable
from app.db.pool import Database
from app.domain.contracts import ChatRequest
from app.workers.memory_worker import MemoryWorker
from tests.fakes import fact, strong_match_answer
from tests.integration.conftest import WEBHOOK_SECRET, Externals, operator_headers

LOG = (
    b"2026-09-29 02:40:36.698  WARN 1 --- [http-nio-8080-exec-132] com.zaxxer.hikari.pool.HikariPool : "
    b"HikariPool-1 - Connection is not available, request timed out after 30000ms.\n"
    b"2026-09-29 02:40:37.001 ERROR 1 --- [main] c.k.payments : password=hunter2-super-secret leaked\n"
)
MESSAGE = "payments-api p99 latency 10s and 5xx at 13%. See the attached log."


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            events.append((name, json.loads("\n".join(data))))
    return events


async def chat(client: httpx.AsyncClient, **body: Any) -> dict[str, dict[str, Any]]:
    response = await client.post("/chat", json={"engineer_id": "priya", "message": MESSAGE, **body})
    assert response.status_code == 200
    return {name: data for name, data in parse_sse(response.text)}


async def upload(
    client: httpx.AsyncClient,
    engineer: str = "priya",
    conversation_id: str | None = None,
    content: bytes = LOG,
) -> dict[str, Any]:
    data = {"engineer_id": engineer, **({"conversation_id": conversation_id} if conversation_id else {})}
    response = await client.post("/files", data=data, files={"file": ("payments.log", content, "text/plain")})
    assert response.status_code == 201, response.text
    return dict(response.json())


def signed(body: bytes) -> dict[str, str]:
    timestamp = int(time.time())
    signature = hmac.new(WEBHOOK_SECRET.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Hindsight-Signature-V2": f"t={timestamp},v1={signature}"}


async def outbox(db: Database) -> list[dict[str, Any]]:
    async with db.transaction() as conn:
        cursor = await conn.execute("select * from memory_outbox order by enqueue_seq")
        return await cursor.fetchall()


async def test_full_demo_journey(
    app_client: httpx.AsyncClient, externals: Externals, db: Database, settings: Settings
) -> None:
    # 1. Upload a diagnostic log (secrets are redacted before storage).
    uploaded = await upload(app_client)
    assert uploaded["redaction_count"] >= 1
    stored = next(iter(externals.storage.objects.values()))
    assert b"hunter2-super-secret" not in stored

    # 2-3. Memory-grounded recommendation with citations, plus the baseline comparison.
    externals.hindsight.structured = strong_match_answer(cite=["INC-2025", "INC-9999"])
    externals.hindsight.facts = [fact("INC-2025"), fact("INC-2025", "Restarting pods did not help")]
    events = await chat(app_client, file_ids=[uploaded["file_id"]])

    assert "baseline" in events and "done" in events
    result = events["recommendation"]
    rec = result["recommendation"]
    assert result["match_strength"] == "strong"
    assert [m["incident_id"] for m in rec["matched_incidents"]] == ["INC-2025"]  # invented INC-9999 removed
    assert "INC-9999" not in json.dumps(result)
    assert rec["recommended_steps"][0]["evidence_ids"] == ["INC-2025"]
    incident_id = result["incident_id"]
    conversation_id = events["conversation"]["conversation_id"]
    assert incident_id and incident_id.startswith("INC-L")

    # 4. Feedback is provisional: nothing reaches the outbox.
    feedback = await app_client.post(
        "/feedback",
        json={
            "engineer_id": "priya",
            "conversation_id": conversation_id,
            "message_id": result["message_id"],
            "step_id": "step-1",
            "outcome": "worked",
            "note": "Suspending recon-job restored the pool within four minutes.",
        },
    )
    assert feedback.status_code == 201
    assert feedback.json()["retain_status"] == "provisional"
    assert await outbox(db) == []

    # 5. The operator resolves the incident and verifies the feedback.
    resolved = await app_client.post(
        f"/incidents/{incident_id}/resolve",
        headers=operator_headers(),
        json={
            "engineer_id": "priya",
            "root_cause": "recon-job held Postgres connections during Midnight Deals.",
            "resolution": "Suspended recon-job and rescheduled it to 04:00 IST.",
            "verified_feedback_ids": [feedback.json()["feedback_id"]],
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["verified_feedback"] == 1
    jobs = await outbox(db)
    assert {job["source_type"] for job in jobs} == {"feedback", "resolution"}

    # 6. The worker submits; the signed webhook completes the jobs.
    worker = MemoryWorker(settings, db, gateway_factory=lambda _bank: externals.hindsight)  # type: ignore[arg-type,return-value]
    assert await worker.process_batch() == 2
    for job in await outbox(db):
        operation_id = job["hindsight_operation_id"]
        externals.hindsight.complete(operation_id, units=5)
        body = json.dumps({"event": "retain.completed", "operation_id": operation_id}).encode()
        delivery = await app_client.post("/webhooks/hindsight", content=body, headers=signed(body))
        assert delivery.json()["status"] == "processed"
    assert {job["status"] for job in await outbox(db)} == {"retained"}
    public_job = (await app_client.get(f"/memory-jobs/{resolved.json()['memory_job_id']}")).json()
    assert public_job["status"] == "retained" and "last_error" not in public_job

    # 7. A related incident may now cite the newly remembered incident.
    externals.hindsight.structured = strong_match_answer(cite=[incident_id])
    externals.hindsight.facts = [fact(incident_id), fact(incident_id, "Rescheduling recon-job fixed it")]
    follow_up = await chat(app_client)
    assert [m["incident_id"] for m in follow_up["recommendation"]["recommendation"]["matched_incidents"]] == [
        incident_id
    ]


async def test_bare_ratings_are_never_promoted(
    app_client: httpx.AsyncClient, externals: Externals, db: Database
) -> None:
    externals.hindsight.structured = strong_match_answer(cite=["INC-2025"])
    externals.hindsight.facts = [fact("INC-2025"), fact("INC-2025", "second")]
    events = await chat(app_client)
    rating = await app_client.post(
        "/feedback",
        json={
            "engineer_id": "priya",
            "conversation_id": events["conversation"]["conversation_id"],
            "message_id": events["recommendation"]["message_id"],
            "step_id": "step-1",
            "rating": -1,
        },
    )
    assert rating.json()["retain_status"] == "not_eligible"
    review = await app_client.post(
        f"/feedback/{rating.json()['feedback_id']}/review",
        headers=operator_headers(),
        json={"reviewer": "arjun", "decision": "verify"},
    )
    assert review.status_code == 200 and review.json()["memory_job_id"] is None
    assert await outbox(db) == []


async def test_attachments_are_isolated_per_conversation(
    app_client: httpx.AsyncClient, externals: Externals
) -> None:
    externals.hindsight.structured = strong_match_answer(cite=["INC-2025"])
    first = await chat(app_client)
    conversation_a = first["conversation"]["conversation_id"]
    owned_by_a = await upload(app_client, conversation_id=conversation_a)
    someone_elses = await upload(app_client, engineer="arjun")

    for file_id in (owned_by_a["file_id"], someone_elses["file_id"]):
        events = await chat(app_client, file_ids=[file_id])  # new conversation
        assert "error" in events and "recommendation" not in events
        assert events["error"]["details"]["file_ids"] == [file_id]


async def test_tampered_attachment_is_reported_not_ignored(
    app_client: httpx.AsyncClient, externals: Externals
) -> None:
    uploaded = await upload(app_client)
    key = next(iter(externals.storage.objects))
    externals.storage.objects[key] = b"tampered contents of the same length?"
    events = await chat(app_client, file_ids=[uploaded["file_id"]])
    assert "recommendation" not in events
    assert (
        "checksum_mismatch" in events["rejected"]["reason"] or "size_mismatch" in events["rejected"]["reason"]
    )
    assert events["attachment_error"]["files"]


async def test_memory_outage_falls_back_with_disclosure(
    app_client: httpx.AsyncClient, externals: Externals
) -> None:
    externals.hindsight.reflect_error = DependencyUnavailable("hindsight down")
    events = await chat(app_client)
    result = events["recommendation"]
    assert result["match_strength"] == "unavailable" and result["memory_available"] is False
    assert "unavailable" in result["recommendation"]["abstention_reason"]
    assert all(step["is_hypothesis"] for step in result["recommendation"]["recommended_steps"])


async def test_missing_context_asks_one_question(app_client: httpx.AsyncClient, externals: Externals) -> None:
    externals.groq.understanding = externals.groq.understanding.model_copy(
        update={
            "service_id": None,
            "error_signatures": [],
            "symptoms": "slow",
            "needs_clarification": True,
            "clarification_question": "Which service is affected?",
        }
    )
    first = await chat(app_client, message="Something is slow")
    assert first["clarification"]["question"] == "Which service is affected?"
    # A second vague message proceeds instead of asking again.
    externals.hindsight.structured = strong_match_answer(cite=["INC-2025"])
    second = await chat(
        app_client,
        message="still slow, latency alert",
        conversation_id=first["conversation"]["conversation_id"],
    )
    assert "clarification" not in second and "recommendation" in second


async def test_off_topic_requests_are_rejected(app_client: httpx.AsyncClient) -> None:
    events = await chat(app_client, message="Write a marketing slogan for a shoe sale.")
    assert "rejected" in events and "recommendation" not in events


async def test_client_disconnect_is_recorded(
    app_client: httpx.AsyncClient, externals: Externals, db: Database
) -> None:
    externals.hindsight.structured = strong_match_answer(cite=["INC-2025"])
    agent = app_client._transport.app.state.container.agent  # type: ignore[attr-defined]
    stream = agent.stream(ChatRequest(engineer_id="priya", message=MESSAGE))
    first = await anext(stream)
    assert first["event"] == "conversation"
    await anext(stream)  # first progress event
    await stream.aclose()  # the browser went away

    async with db.transaction() as conn:
        cursor = await conn.execute("select status, error from agent_runs")
        runs = await cursor.fetchall()
    assert runs == [{"status": "failed", "error": "client_disconnected"}]
