"""POST /feedback (provisional) and POST /feedback/{feedback_id}/review (operator only)."""

from __future__ import annotations

from fastapi import APIRouter, Path, status

from app.api.deps import ContainerDep
from app.api.security import OperatorOnly
from app.domain.contracts import (
    ID_PATTERN,
    FeedbackRequest,
    FeedbackResponse,
    FeedbackReviewRequest,
    FeedbackReviewResponse,
)

router = APIRouter(tags=["feedback"])


@router.post("/feedback", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
async def create_feedback(body: FeedbackRequest, container: ContainerDep) -> FeedbackResponse:
    """Stores feedback as provisional. It never reaches memory until it is verified."""
    return await container.feedback.submit(body)


@router.post("/feedback/{feedback_id}/review", response_model=FeedbackReviewResponse)
async def review(
    body: FeedbackReviewRequest,
    container: ContainerDep,
    _: OperatorOnly,
    feedback_id: str = Path(pattern=ID_PATTERN),
) -> FeedbackReviewResponse:
    """Verify or reject provisional feedback. Verified feedback with an outcome is queued for memory."""
    return await container.feedback.review(feedback_id, body)
