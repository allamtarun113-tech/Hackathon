"""Scratch Postgres database for workflow evaluation.

Each run creates a fresh database, applies every migration from empty, seeds the
reference data and the 500 historical incidents, and drops it afterwards. Production
data is never touched.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg import sql

from app.core.config import REPO_ROOT, Settings
from app.db.pool import Database
from app.db.repositories import deployments
from app.importers.incidents import import_incidents
from app.importers.reference import import_reference
from app.importers.source import SeedSource
from evals.workflow.cases import DeploymentSeed

MIGRATIONS = sorted((REPO_ROOT / "supabase" / "migrations").glob("*.sql"))


def _with_database(url: str, name: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{name}"


@contextmanager
def scratch_database(admin_url: str, *, keep: bool = False) -> Iterator[str]:
    name = f"pagermind_wf_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    url = _with_database(admin_url, name)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for path in MIGRATIONS:
                conn.execute(path.read_text().encode())
        yield url
    finally:
        if not keep:
            with psycopg.connect(admin_url, autocommit=True) as conn:
                conn.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


async def seed_reference_data(settings: Settings) -> None:
    """Teams, engineers, services and the historical incidents that answers may cite."""
    db = Database(settings, role="cli")
    await db.open()
    try:
        source = SeedSource(settings.data_dir)
        await import_reference(db, source)
        await import_incidents(db, source)
    finally:
        await db.close()


async def seed_deployments(db: Database, seeds: list[DeploymentSeed]) -> None:
    now = datetime.now(UTC)
    async with db.transaction() as conn:
        for seed in seeds:
            await deployments.upsert_deployment(
                conn,
                {
                    "deployment_id": seed.deployment_id,
                    "service_id": seed.service_id,
                    "version": seed.version,
                    "previous_version": None,
                    "change_type": "deploy",
                    "change_summary": seed.change_summary,
                    "deployed_by": "workflow-eval",
                    "environment": "production",
                    "region": None,
                    "deployed_at": now - timedelta(hours=seed.hours_ago),
                },
            )
