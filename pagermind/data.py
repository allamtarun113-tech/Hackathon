"""Reads the synthetic dataset in data/: demo alerts, the incident history (loaded into Hindsight
by scripts/seed_memory.py and shown on the History tab) and the service catalog.

Neither answer reads past incidents from here - the 'with Hindsight' answer only knows what
Hindsight remembers, and the 'without Hindsight' answer gets just the alert and its logs.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from pagermind.config import get_config

DATA: Path = get_config().data_dir


@lru_cache
def catalog() -> dict[str, Any]:
    return yaml.safe_load((DATA / "service_catalog.yaml").read_text())


@lru_cache
def scenarios() -> list[dict[str, Any]]:
    return json.loads((DATA / "demo_scenarios.json").read_text())


@lru_cache
def history() -> list[dict[str, Any]]:
    return json.loads((DATA / "incidents_history.json").read_text())
