"""Load the 100 held-out scenarios into `evaluation_cases` (CI only; never into memory)."""

from __future__ import annotations

from app.db.pool import Database
from app.db.repositories import evaluation
from app.importers.source import SeedSource


async def import_evaluation_cases(db: Database, source: SeedSource) -> dict[str, int]:
    async with db.transaction() as conn:
        for scenario in source.scenarios:
            await evaluation.upsert_case(
                conn,
                case_id=scenario["id"],
                category=scenario.get("case_type", "unknown"),
                service_id=scenario.get("service"),
                prompt={
                    "alert": scenario["alert"],
                    "log_excerpt": scenario.get("log_excerpt", []),
                    "severity": scenario.get("severity"),
                },
                expected=scenario.get("expected") or {},
            )
    return {"evaluation_cases": len(source.scenarios)}
