"""POST /files - upload a log file (multipart)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile, status

from app.api.deps import ContainerDep
from app.domain.contracts import ID_PATTERN, FileUploadResponse

router = APIRouter(tags=["files"])


@router.post("/files", response_model=FileUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    container: ContainerDep,
    file: Annotated[UploadFile, File(description="A plain-text log file (.log, .txt, .json, .jsonl).")],
    engineer_id: Annotated[str, Form(pattern=ID_PATTERN)],
    conversation_id: Annotated[str | None, Form(pattern=ID_PATTERN)] = None,
) -> FileUploadResponse:
    data = await file.read(container.settings.max_upload_bytes + 1)
    return await container.files.store_log(
        filename=file.filename or "upload.log",
        data=data,
        engineer_id=engineer_id,
        conversation_id=conversation_id,
    )
