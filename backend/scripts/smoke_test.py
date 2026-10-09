"""Post-deployment smoke test.

    python scripts/smoke_test.py --api https://pagermind-api.onrender.com --web https://pagermind.vercel.app
    python scripts/smoke_test.py --api ... --full    # also runs one real diagnosis (uses Hindsight + Groq)

Exits non-zero on the first failed check. Uses only safe, idempotent requests by default.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx


@dataclass
class Check:
    name: str
    run: Callable[[], str]


class SmokeFailure(Exception):
    pass


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def read_sse(
    client: httpx.Client, url: str, body: dict[str, object], timeout: float
) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event = "message"
    with client.stream("POST", url, json=body, timeout=timeout) as response:
        expect(response.status_code == 200, f"/chat returned HTTP {response.status_code}")
        for line in response.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                events.append((event, json.loads(line[5:].strip())))
    return events


def build_checks(api: str, web: str | None, full: bool) -> list[Check]:
    client = httpx.Client(timeout=20)
    engineer: dict[str, str] = {}

    def liveness() -> str:
        response = client.get(f"{api}/health/live")
        expect(response.status_code == 200, f"HTTP {response.status_code}")
        return "ok"

    def readiness() -> str:
        response = client.get(f"{api}/health/ready")
        expect(response.status_code == 200, f"not ready: {response.text[:200]}")
        return "supabase reachable"

    def health() -> str:
        response = client.get(f"{api}/health")
        body = response.json()
        expect(body["worker"]["status"] == "healthy", f"memory worker: {body['worker']}")
        expect(body["status"] in ("healthy", "degraded"), f"status={body['status']}")
        text = response.text.lower()
        expect(
            not any(secret in text for secret in ("service_role", "gsk_", "sk-lf-")), "health leaks secrets"
        )
        hindsight = body["dependencies"].get("hindsight", {}).get("status", "no snapshot yet")
        return f"{body['status']}; worker healthy; hindsight={hindsight}"

    def operator_boundary() -> str:
        reads = client.get(f"{api}/conversations/CONV-SMOKE")
        writes = client.post(f"{api}/incidents/INC-SMOKE/resolve", json={})
        expect(reads.status_code in (401, 503), f"internal read exposed (HTTP {reads.status_code})")
        expect(writes.status_code in (401, 503), f"resolve not protected (HTTP {writes.status_code})")
        return "privileged endpoints refuse anonymous callers"

    def request_id() -> str:
        response = client.get(f"{api}/health/live")
        expect(bool(response.headers.get("x-request-id")), "missing X-Request-ID header")
        return response.headers["x-request-id"]

    def engineers() -> str:
        response = client.get(f"{api}/engineers")
        expect(response.status_code == 200 and response.json(), "no engineers returned")
        engineer.update(response.json()[0])
        return f"{len(response.json())} engineers"

    def validation() -> str:
        response = client.post(f"{api}/feedback", json={"unexpected": True})
        expect(response.status_code == 422, f"expected 422, got {response.status_code}")
        return "422 on invalid body"

    def not_found() -> str:
        response = client.get(f"{api}/memory-jobs/MEMJOB-DOES-NOT-EXIST")
        expect(response.status_code == 404, f"expected 404, got {response.status_code}")
        return "404"

    def webhook_signature() -> str:
        response = client.post(
            f"{api}/webhooks/hindsight",
            content=b'{"event":"retain.completed"}',
            headers={"X-Hindsight-Signature-V2": f"t={int(time.time())},v1=invalid"},
        )
        expect(response.status_code == 401, f"unsigned webhook accepted (HTTP {response.status_code})")
        return "invalid signature rejected"

    def cors() -> str:
        if not web:
            return "skipped (no --web)"
        allowed = client.options(
            f"{api}/chat", headers={"Origin": web, "Access-Control-Request-Method": "POST"}
        )
        denied = client.options(
            f"{api}/chat", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
        )
        expect(allowed.headers.get("access-control-allow-origin") == web, "frontend origin not allowed")
        expect("access-control-allow-origin" not in denied.headers, "foreign origin allowed")
        return "frontend allowed, others denied"

    def off_topic_chat() -> str:
        events = read_sse(
            client,
            f"{api}/chat",
            {"engineer_id": engineer["engineer_id"], "message": "Write a slogan for a shoe sale."},
            timeout=30,
        )
        names = [name for name, _ in events]
        expect("rejected" in names and "done" in names, f"unexpected events {names}")
        return "guardrail rejected off-topic request"

    def full_chat() -> str:
        started = time.monotonic()
        events = read_sse(
            client,
            f"{api}/chat",
            {
                "engineer_id": engineer["engineer_id"],
                "message": "payments-api p99 latency 10s and 5xx at 13%. Logs: HikariPool-1 - Connection is not "
                "available, request timed out after 30000ms.",
            },
            timeout=90,
        )
        names = [name for name, _ in events]
        expect("recommendation" in names, f"no recommendation: {names}")
        recommendation = next(data for name, data in events if name == "recommendation")
        return f"match={recommendation['match_strength']} in {time.monotonic() - started:.1f}s"

    def frontend() -> str:
        if not web:
            return "skipped (no --web)"
        response = client.get(web)
        expect(response.status_code == 200 and "PagerMind" in response.text, "frontend not serving")
        expect("x-frame-options" in response.headers, "security headers missing")
        return "ok"

    checks = [
        Check("API liveness", liveness),
        Check("API readiness", readiness),
        Check("Worker heartbeat and health snapshot", health),
        Check("Operator boundary", operator_boundary),
        Check("Request ID header", request_id),
        Check("Engineer list", engineers),
        Check("Request validation", validation),
        Check("Not-found handling", not_found),
        Check("Webhook signature verification", webhook_signature),
        Check("CORS policy", cors),
        Check("Off-topic guardrail over SSE", off_topic_chat),
        Check("Frontend", frontend),
    ]
    if full:
        checks.append(Check("End-to-end diagnosis", full_chat))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--api", required=True, help="Backend base URL")
    parser.add_argument("--web", help="Frontend base URL")
    parser.add_argument("--full", action="store_true", help="Run one real diagnosis")
    args = parser.parse_args()

    for check in build_checks(args.api.rstrip("/"), args.web.rstrip("/") if args.web else None, args.full):
        try:
            detail = check.run()
        except Exception as error:
            print(f"FAIL  {check.name}: {error}")
            return 1
        print(f"PASS  {check.name}: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
