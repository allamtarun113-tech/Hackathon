"""Typed access to the seed dataset under data/."""

from __future__ import annotations

import json
import re
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml


def parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def team_id_for(name: str) -> str:
    return "TEAM-" + slugify(name).upper()


class SeedSource:
    """Lazily loads and caches each dataset file."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.production_dir = data_dir / "production"

    def _json(self, relative: str) -> Any:
        return json.loads((self.data_dir / relative).read_text())

    @cached_property
    def catalog(self) -> dict[str, Any]:
        catalog: dict[str, Any] = yaml.safe_load((self.data_dir / "service_catalog.yaml").read_text())
        return catalog

    @cached_property
    def engineers(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self._json("engineers.json")
        return rows

    @cached_property
    def deployments(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self._json("deploys.json")
        return rows

    @cached_property
    def incidents(self) -> list[dict[str, Any]]:
        incidents: list[dict[str, Any]] = self._json("incidents_history.json")
        # Only the memory split is historical knowledge; anything else is evaluation material.
        return [i for i in incidents if i.get("data_provenance", {}).get("split", "memory") == "memory"]

    @cached_property
    def scenarios(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self._json("demo_scenarios.json")
        return rows

    def production(self, name: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self._json(f"production/{name}.json")
        return rows

    @cached_property
    def user_to_engineer(self) -> dict[str, str]:
        return {user["user_id"]: user["engineer_id"] for user in self.production("users")}

    def runbook_paths(self) -> list[Path]:
        return sorted((self.data_dir / "runbooks").glob("*.md"))

    def log_path(self, relative: str) -> Path:
        path = (self.data_dir / relative).resolve()
        if self.data_dir.resolve() not in path.parents:
            raise ValueError(f"Log path escapes the data directory: {relative}")
        return path
