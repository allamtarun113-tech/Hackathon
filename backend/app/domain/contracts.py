"""Request and response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$"
FeedbackOutcome = Literal["worked", "partial", "did_not_work", "made_worse", "not_tried"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------- chat
class ChatRequest(StrictModel):
    engineer_id: str = Field(pattern=ID_PATTERN)
    message: str = Field(min_length=1, max_length=20_000)
    conversation_id: str | None = Field(default=None, pattern=ID_PATTERN)
    file_ids: list[str] = Field(default_factory=list, max_length=3)


# --------------------------------------------------------------------------- feedback
class FeedbackRequest(StrictModel):
    engineer_id: str = Field(pattern=ID_PATTERN)
    conversation_id: str = Field(pattern=ID_PATTERN)
    message_id: str = Field(pattern=ID_PATTERN)
    step_id: str | None = Field(default=None, max_length=64)
    rating: Literal[-1, 1] | None = None
    outcome: FeedbackOutcome | None = None
    note: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def _has_signal(self) -> FeedbackRequest:
        if self.rating is None and self.outcome is None and not self.note:
            raise ValueError("Provide a rating, an outcome or a note.")
        return self


class FeedbackResponse(BaseModel):
    feedback_id: str
    validation_state: Literal["pending"]
    retain_status: Literal["provisional", "not_eligible"]
    reason: str


class FeedbackReviewRequest(StrictModel):
    reviewer: str = Field(pattern=ID_PATTERN, description="Engineer ID of the reviewer (attribution).")
    decision: Literal["verify", "reject"]
    note: str | None = Field(default=None, max_length=1_000)


class FeedbackReviewResponse(BaseModel):
    feedback_id: str
    validation_state: Literal["verified", "rejected"]
    memory_job_id: str | None


# --------------------------------------------------------------------------- incidents
class ResolveIncidentRequest(StrictModel):
    engineer_id: str = Field(pattern=ID_PATTERN)
    root_cause: str = Field(min_length=10, max_length=2_000)
    resolution: str = Field(min_length=10, max_length=4_000)
    commands: list[str] = Field(default_factory=list, max_length=20)
    failed_actions: list[str] = Field(default_factory=list, max_length=20)
    follow_ups: list[str] = Field(default_factory=list, max_length=20)
    verified_feedback_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Provisional feedback the resolver confirms as accurate.",
    )
    rejected_feedback_ids: list[str] = Field(default_factory=list, max_length=50)
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def _disjoint(self) -> ResolveIncidentRequest:
        if set(self.verified_feedback_ids) & set(self.rejected_feedback_ids):
            raise ValueError("A feedback item cannot be both verified and rejected.")
        return self


class ResolveIncidentResponse(BaseModel):
    incident_id: str
    postmortem_version: int
    memory_job_id: str
    verified_feedback: int
    rejected_feedback: int
    feedback_memory_job_ids: list[str]


# --------------------------------------------------------------------------- files
class FileUploadResponse(BaseModel):
    file_id: str
    sha256: str
    size_bytes: int
    mime_type: str
    redaction_count: int
    error_signatures: list[str]


# --------------------------------------------------------------------------- memory jobs
class MemoryJobResponse(BaseModel):
    """Public view: enough to show ingestion progress, no internal error details."""

    job_id: str
    status: str
    attempt_count: int
    max_attempts: int
    memory_unit_count: int | None
    created_at: datetime
    submitted_at: datetime | None
    completed_at: datetime | None


class MemoryJobDetail(MemoryJobResponse):
    """Operator view."""

    document_id: str
    source_type: str
    source_id: str
    last_error: str | None
    rejection_reason: str | None


# --------------------------------------------------------------------------- health
class DependencyHealth(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy", "not_configured"]
    latency_ms: int | None = None
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, Literal["ok", "failed"]]


class HealthResponse(BaseModel):
    """Latest snapshot written by the memory worker, plus worker liveness. Read-only."""

    status: Literal["healthy", "degraded", "unhealthy", "unknown"]
    version: str
    environment: str
    snapshot_at: datetime | None
    dependencies: dict[str, DependencyHealth]
    memory_jobs: dict[str, int]
    worker: DependencyHealth
