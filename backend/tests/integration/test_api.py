"""HTTP surface: health, error envelopes, the operator boundary and rate limiting."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from tests.integration.conftest import Externals, build_client, operator_headers


async def test_health_endpoints_are_separated(app_client: httpx.AsyncClient) -> None:
    live = await app_client.get("/health/live")
    assert live.status_code == 200 and live.json() == {"status": "ok"}
    assert live.headers["x-request-id"]

    ready = await app_client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["checks"] == {"supabase": "ok"}

    detail = (await app_client.get("/health")).json()
    assert detail["status"] == "unknown"  # no worker snapshot yet
    assert detail["worker"]["status"] == "unhealthy"


async def test_health_requests_never_write(app_client: httpx.AsyncClient, settings: Settings) -> None:
    for path in ("/health", "/health/ready", "/health/live"):
        await app_client.get(path)
    with psycopg.connect(settings.database_url.get_secret_value()) as conn:
        assert conn.execute("select count(*) from health_snapshots").fetchone() == (0,)


async def test_validation_and_not_found_use_the_error_envelope(app_client: httpx.AsyncClient) -> None:
    invalid = await app_client.post("/feedback", json={"unexpected": True})
    assert invalid.status_code == 422

    missing = await app_client.get("/memory-jobs/MEMJOB-MISSING")
    assert missing.status_code == 404
    error = missing.json()["error"]
    assert error["code"] == "not_found" and error["request_id"] == missing.headers["x-request-id"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/conversations/CONV-1"),
        ("GET", "/incidents/INC-2025"),
        ("POST", "/incidents/INC-2025/resolve"),
        ("POST", "/feedback/FDB-1/review"),
        ("GET", "/memory-jobs"),
    ],
)
async def test_privileged_endpoints_require_the_operator_token(
    app_client: httpx.AsyncClient, method: str, path: str
) -> None:
    body = {} if method == "POST" else None
    anonymous = await app_client.request(method, path, json=body)
    wrong = await app_client.request(method, path, json=body, headers={"Authorization": "Bearer wrong"})
    assert anonymous.status_code == 401
    assert wrong.status_code == 401


async def test_operator_can_read_incident(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/incidents/INC-2025", headers=operator_headers())
    assert response.status_code == 200
    assert response.json()["incident_id"] == "INC-2025"


@pytest.fixture
async def locked_down_client(settings: Settings, externals: Externals) -> AsyncIterator[httpx.AsyncClient]:
    """Same app, but with no operator token configured and a tiny rate limit."""
    restricted = settings.model_copy(update={"operator_token": SecretStr(""), "rate_limit_per_minute": 3})
    async with build_client(restricted, externals) as client:
        yield client


async def test_operator_endpoints_fail_closed_without_a_token(locked_down_client: httpx.AsyncClient) -> None:
    response = await locked_down_client.get("/incidents/INC-2025", headers=operator_headers())
    assert response.status_code == 503


async def test_rate_limit_is_shared_and_ignores_spoofed_forwarding(
    locked_down_client: httpx.AsyncClient,
) -> None:
    statuses = []
    for index in range(5):
        response = await locked_down_client.post(
            "/feedback",
            json={"unexpected": True},
            headers={"X-Forwarded-For": f"198.51.100.{index}"},  # spoofed: TRUSTED_PROXY_HOPS=0
        )
        statuses.append(response.status_code)
    assert statuses[:3] == [422, 422, 422]
    assert statuses[3:] == [429, 429]
