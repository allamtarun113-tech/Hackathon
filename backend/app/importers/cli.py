"""Deterministic, re-runnable seed import.

pagermind-import all                 # everything, in dependency order
pagermind-import reference           # teams, engineers, services, owners, shifts
pagermind-import incidents           # deployments, incidents, change links
pagermind-import conversations       # conversations, messages, checkpoints, feedback
pagermind-import files               # incident logs + runbooks to Storage
pagermind-import memory              # queue incident + runbook documents for Hindsight
pagermind-import evaluation          # held-out scenarios into evaluation_cases (CI)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import typer

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.pool import Database
from app.importers import conversations, evaluation, files, incidents, memory, reference
from app.importers.source import SeedSource
from app.storage.supabase_storage import SupabaseStorage

app = typer.Typer(help="Import the PagerMind seed dataset into Supabase.", no_args_is_help=True)
log = get_logger(__name__)

Step = Callable[[Database, SupabaseStorage, SeedSource, Settings], Awaitable[dict[str, int]]]

STEPS: dict[str, list[Step]] = {
    "reference": [lambda db, _s, src, _c: reference.import_reference(db, src)],
    "incidents": [
        lambda db, _s, src, _c: incidents.import_deployments(db, src),
        lambda db, _s, src, _c: incidents.import_incidents(db, src),
        lambda db, _s, src, _c: incidents.import_change_links(db, src),
    ],
    "conversations": [
        lambda db, _s, src, _c: conversations.import_conversations(db, src),
        lambda db, _s, src, _c: conversations.import_feedback(db, src),
        lambda db, _s, src, _c: conversations.import_observability_fixtures(db, src),
    ],
    "files": [files.import_incident_logs, files.import_runbooks],
    "memory": [
        lambda db, _s, src, cfg: memory.queue_incident_memories(db, src, cfg.hindsight_bank_id),
        lambda db, _s, src, cfg: memory.queue_runbook_memories(db, src, cfg.hindsight_bank_id),
    ],
    "evaluation": [lambda db, _s, src, _c: evaluation.import_evaluation_cases(db, src)],
}
ORDER = ["reference", "incidents", "conversations", "files", "memory", "evaluation"]


async def _run(groups: list[str]) -> None:
    settings = get_settings()
    db = Database(settings)
    storage = SupabaseStorage(settings)
    source = SeedSource(settings.data_dir)
    await db.open()
    try:
        for group in groups:
            for step in STEPS[group]:
                counts: dict[str, Any] = await step(db, storage, source, settings)
                log.info("import_step_completed", group=group, **counts)
    finally:
        await storage.close()
        await db.close()


def _command(group: str) -> Callable[[], None]:
    def command() -> None:
        configure_logging(get_settings().log_level, json_output=False)
        asyncio.run(_run([group]))

    command.__doc__ = f"Import the '{group}' group."
    return command


for _group in ORDER:
    app.command(name=_group)(_command(_group))


@app.command(name="all")
def import_all() -> None:
    """Import every group in dependency order."""
    configure_logging(get_settings().log_level, json_output=False)
    asyncio.run(_run(ORDER))


if __name__ == "__main__":
    app()
