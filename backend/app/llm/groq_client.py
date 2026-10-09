"""Groq chat client with JSON-mode output, retries and model fallback.

Used for incident understanding and for the plain-LLM baseline. It never writes
the final memory-grounded answer; that comes from Hindsight reflect.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from functools import partial
from typing import Literal

import groq
from groq import AsyncGroq
from groq.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.errors import DependencyUnavailable
from app.core.logging import get_logger
from app.core.resilience import BreakerStore, CircuitBreaker, call_with_resilience
from app.observability.tracing import record_generation

log = get_logger(__name__)

ReasoningEffort = Literal["none", "default", "low", "medium", "high"]


@dataclass(frozen=True)
class LLMResult[M: BaseModel]:
    value: M
    model: str
    fallback_used: bool
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


def _retryable(error: BaseException) -> bool:
    return isinstance(error, (groq.RateLimitError, groq.APIConnectionError, groq.InternalServerError))


class GroqClient:
    def __init__(self, settings: Settings, *, breaker_store: BreakerStore | None = None) -> None:
        self._client = AsyncGroq(
            api_key=settings.groq_api_key.get_secret_value(),
            timeout=settings.groq_timeout_seconds,
            max_retries=0,  # retries are handled by call_with_resilience
        )
        self._timeout = settings.groq_timeout_seconds
        self.models = [settings.groq_primary_model, settings.groq_fallback_model]
        self._breakers = {
            model: CircuitBreaker(f"groq:{model}", store=breaker_store) for model in self.models
        }

    async def close(self) -> None:
        await self._client.close()

    @staticmethod
    def _reasoning_effort(model: str) -> ReasoningEffort | None:
        """Keep reasoning short; Qwen3 thinking output would break JSON mode."""
        if "gpt-oss" in model:
            return "low"
        if "qwen3" in model:
            return "none"
        return None

    async def _create(
        self, model: str, messages: list[ChatCompletionMessageParam], *, max_tokens: int, temperature: float
    ) -> ChatCompletion:
        return await self._client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format={"type": "json_object"},
            reasoning_effort=self._reasoning_effort(model),
        )

    async def complete_json[M: BaseModel](
        self,
        *,
        name: str,
        system: str,
        user: str,
        schema: type[M],
        max_tokens: int = 2000,
        temperature: float = 0.1,
    ) -> LLMResult[M]:
        """Return a validated `schema` instance, falling back to the secondary model on failure."""
        last_error: Exception | None = None
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        for index, model in enumerate(self.models):
            started = time.perf_counter()
            try:
                response = await call_with_resilience(
                    partial(self._create, model, messages, max_tokens=max_tokens, temperature=temperature),
                    breaker=self._breakers[model],
                    attempt_timeout=self._timeout,
                    attempts=2,
                    retry_on=_retryable,
                )
                content = response.choices[0].message.content or ""
                value = schema.model_validate(json.loads(content))
            except (json.JSONDecodeError, ValidationError) as error:
                last_error = error
                log.warning("llm_invalid_output", model=model, call=name, error_type=type(error).__name__)
                continue
            except Exception as error:
                last_error = error
                log.warning("llm_call_failed", model=model, call=name, error_type=type(error).__name__)
                continue

            latency_ms = int((time.perf_counter() - started) * 1000)
            usage = response.usage
            result = LLMResult(
                value=value,
                model=model,
                fallback_used=index > 0,
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
                latency_ms=latency_ms,
            )
            record_generation(
                name=name,
                model=model,
                input_text=user,
                output=value.model_dump(),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                fallback_used=result.fallback_used,
            )
            return result

        raise DependencyUnavailable(
            f"All Groq models failed for '{name}'.",
            details={"error_type": type(last_error).__name__ if last_error else None},
        )
