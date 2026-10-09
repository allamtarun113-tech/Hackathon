"""GET /engineers and GET /services - lookup data for the UI."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import ContainerDep
from app.db.repositories import reference

router = APIRouter(tags=["reference"])


@router.get("/engineers")
async def list_engineers(container: ContainerDep) -> list[dict[str, Any]]:
    """Seeded engineers. Selection in the UI is attribution only, not identity verification."""
    async with container.db.transaction() as conn:
        return await reference.list_engineers(conn)


@router.get("/services")
async def list_services(container: ContainerDep) -> list[dict[str, Any]]:
    async with container.db.transaction() as conn:
        return await reference.list_services(conn)
