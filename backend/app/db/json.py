"""JSONB parameters that serialise datetimes, UUIDs and decimals consistently."""

from __future__ import annotations

import json
from functools import partial
from typing import Any

from psycopg.types.json import Jsonb

_dumps = partial(json.dumps, default=str, ensure_ascii=False)


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value, dumps=_dumps)
