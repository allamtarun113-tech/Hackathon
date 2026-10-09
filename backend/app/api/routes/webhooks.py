"""POST /webhooks/hindsight - signed, at-least-once delivery of Hindsight events."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.deps import ContainerDep

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/hindsight")
async def hindsight_webhook(request: Request, container: ContainerDep) -> JSONResponse:
    body = await request.body()
    outcome = await container.webhooks.handle(body=body, headers=dict(request.headers))
    # Any 2xx stops redelivery; invalid signatures get 401 so misconfiguration is visible in Hindsight.
    return JSONResponse(status_code=outcome.http_status, content={"status": outcome.status})
