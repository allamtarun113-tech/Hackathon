from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import REPO_ROOT


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return REPO_ROOT / "data"
