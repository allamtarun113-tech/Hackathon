"""Langfuse tracing.

One trace per chat message; one span per LangGraph node; generations for Groq
calls and a span for Hindsight reflect. Every function is a no-op when Langfuse
is not configured, so the rest of the code never checks.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

_langfuse: Any | None = None


def init_tracing(settings: Settings) -> None:
    global _langfuse
    if not settings.langfuse_enabled:
        log.info("langfuse_disabled")
        return
    from langfuse import Langfuse

    _langfuse = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        host=settings.langfuse_host,
        environment=settings.app_env,
    )


def shutdown_tracing() -> None:
    if _langfuse is not None:
        _langfuse.flush()


@contextmanager
def trace_request(
    *, name: str, request_id: str, conversation_id: str, metadata: dict[str, Any]
) -> Iterator[str | None]:
    """Root span for one chat message. Yields the Langfuse trace ID (or None when disabled)."""
    if _langfuse is None:
        yield None
        return
    from langfuse import propagate_attributes

    with (
        _langfuse.start_as_current_observation(
            name=name, as_type="span", metadata={"request_id": request_id, **metadata}
        ) as span,
        propagate_attributes(
            session_id=conversation_id, tags=["pagermind", "chat"], metadata={"request_id": request_id}
        ),
    ):
        yield span.trace_id


@contextmanager
def trace_span(name: str, *, input: Any = None, metadata: dict[str, Any] | None = None) -> Iterator[Any]:
    if _langfuse is None:
        yield None
        return
    with _langfuse.start_as_current_observation(
        name=name, as_type="span", input=input, metadata=metadata or {}
    ) as span:
        yield span


def update_span(
    span: Any, *, output: Any = None, metadata: dict[str, Any] | None = None, level: str | None = None
) -> None:
    if span is None:
        return
    kwargs: dict[str, Any] = {"output": output, "metadata": metadata}
    if level:
        kwargs["level"] = level
    span.update(**{k: v for k, v in kwargs.items() if v is not None})


def record_generation(
    *,
    name: str,
    model: str,
    input_text: str,
    output: Any,
    input_tokens: int | None,
    output_tokens: int | None,
    fallback_used: bool,
) -> None:
    if _langfuse is None:
        return
    with _langfuse.start_as_current_observation(
        name=name,
        as_type="generation",
        model=model,
        input=input_text[:4000],
        metadata={"fallback_used": fallback_used},
    ) as generation:
        generation.update(
            output=output,
            usage_details={"input": input_tokens or 0, "output": output_tokens or 0},
        )


def score_trace(trace_id: str, *, name: str, value: float, comment: str | None = None) -> None:
    """Attach feedback or final incident outcome to the original trace."""
    if _langfuse is None or not trace_id:
        return
    _langfuse.create_score(trace_id=trace_id, name=name, value=value, comment=comment)
