"""Integration fixtures: a fresh Postgres database built from the migrations, plus fake externals.

Requires TEST_DATABASE_URL pointing at a Postgres 15+ server with permission to create
databases, e.g. postgresql://postgres:postgres@localhost:5432/postgres. Tests are skipped
when it is not set.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from psycopg import sql

from app.container import Container
from app.core.config import REPO_ROOT, Settings
from app.db.pool import Database
from app.main import create_app
from tests.fakes import FakeGroq, FakeHindsight, FakeStorage

MIGRATIONS = sorted((REPO_ROOT / "supabase" / "migrations").glob("*.sql"))
OPERATOR_TOKEN = "test-operator-token-0123456789"
WEBHOOK_SECRET = "whsec_test_secret"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Everything under tests/integration is an integration test."""
    for item in items:
        if "tests/integration/" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


def _admin_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    return url


def _with_database(url: str, name: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{name}"


def apply_migrations(url: str, files: list[Path] = MIGRATIONS) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        for path in files:
            conn.execute(path.read_text())  # type: ignore[call-overload]


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """A brand-new database per test session, migrated from empty."""
    admin = _admin_url()
    name = f"pagermind_it_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    url = _with_database(admin, name)
    try:
        apply_migrations(url)
        yield url
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


SEED_SQL = """
insert into teams (team_id, name, slug) values ('TEAM-PAYMENTS', 'Payments', 'payments');
insert into engineers (engineer_id, display_name, team_id) values
    ('priya', 'Priya Sharma', 'TEAM-PAYMENTS'),
    ('arjun', 'Arjun Mehta', 'TEAM-PAYMENTS');
insert into services (service_id, team_id, depends_on) values
    ('payments-api', 'TEAM-PAYMENTS', '{postgres-main}'),
    ('checkout-web', 'TEAM-PAYMENTS', '{}');
insert into incidents (incident_id, service_id, title, severity, status, source, started_at, root_cause)
values ('INC-2025', 'payments-api', 'HikariCP pool exhausted by recon-job', 'SEV1', 'resolved',
        'historical-seed', '2026-01-10T02:40:00Z', 'recon-job held connections');
insert into deployments (deployment_id, service_id, version, change_summary, deployed_at)
values ('DEP-1', 'payments-api', 'v2.4.0', 'Pool size tweak', now() - interval '2 hours');
"""


def _tables(conn: psycopg.Connection[Any]) -> list[str]:
    return [row[0] for row in conn.execute("select tablename from pg_tables where schemaname = 'public'")]


@pytest.fixture
def clean_database(database_url: str) -> str:
    """Empty every table (TRUNCATE bypasses the append-only audit trigger) and seed reference rows."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        tables = ", ".join(f"public.{name}" for name in _tables(conn))
        conn.execute(f"truncate {tables} restart identity cascade")  # type: ignore[call-overload]
        conn.execute(SEED_SQL)  # type: ignore[call-overload]
    return database_url


@pytest.fixture
def settings(clean_database: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url=clean_database,
        hindsight_bank_id="pagermind-test",
        hindsight_webhook_secret=WEBHOOK_SECRET,
        operator_token=OPERATOR_TOKEN,
        trusted_proxy_hops=0,
        rate_limit_per_minute=1_000,
        strong_match_min_facts=2,
        request_timeout_seconds=20,
    )


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings)
    await database.open()
    try:
        yield database
    finally:
        await database.close()


class Externals:
    def __init__(self) -> None:
        self.storage = FakeStorage()
        self.hindsight = FakeHindsight()
        self.groq = FakeGroq()


@pytest.fixture
def externals() -> Externals:
    return Externals()


@asynccontextmanager
async def build_client(settings: Settings, externals: Externals) -> AsyncIterator[httpx.AsyncClient]:
    """Run the real app (lifespan included) with fake externals and a real database."""

    async def factory(cfg: Settings) -> Container:
        database = Database(cfg, role="api")
        await database.open()
        return Container.build(
            cfg,
            db=database,
            storage=externals.storage,  # type: ignore[arg-type]
            hindsight=externals.hindsight,  # type: ignore[arg-type]
            groq=externals.groq,  # type: ignore[arg-type]
        )

    app = create_app(settings, container_factory=factory)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


@pytest.fixture
async def app_client(settings: Settings, externals: Externals) -> AsyncIterator[httpx.AsyncClient]:
    async with build_client(settings, externals) as client:
        yield client


def operator_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
