"""Application settings loaded from environment variables (or the repository-root .env)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Runtime
    app_env: Literal["development", "test", "staging", "production"] = "development"
    log_level: str = "INFO"
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )
    data_dir: Path = REPO_ROOT / "data"

    # Request limits
    max_request_bytes: int = 256 * 1024
    max_message_chars: int = 4_000
    max_log_chars: int = 200_000
    max_upload_bytes: int = 5 * 1024 * 1024
    rate_limit_per_minute: int = 30
    request_timeout_seconds: float = 60.0
    # Number of reverse proxies in front of the API that append to X-Forwarded-For (Render: 1).
    # 0 means trust only the socket peer address.
    trusted_proxy_hops: int = 1

    # Write boundary: one deployment-held secret for endpoints that change canonical data or memory.
    # Empty disables those endpoints entirely (fail closed).
    operator_token: SecretStr = SecretStr("")

    # Attachments
    upload_claim_ttl_minutes: int = 60

    # Agent runtime limits
    agent_max_steps: int = 12
    # Concurrent chat turns per API instance; keep below database_pool_max_size.
    max_concurrent_agent_runs: int = 6
    # Writes, configuration and status calls. Reflect has its own, tighter limits below.
    hindsight_timeout_seconds: float = 45.0
    # Reflect is never retried: on timeout the turn degrades to the baseline instead of waiting twice.
    # `mid` must fit the 15s p95 turn target; SEV1 (`high` budget) trades latency for a deeper search.
    hindsight_reflect_timeout_seconds: float = 12.0
    hindsight_reflect_high_timeout_seconds: float = 25.0
    groq_timeout_seconds: float = 30.0
    strong_match_min_facts: int = 2

    # Supabase
    database_url: SecretStr = SecretStr("")
    database_pool_min_size: int = 1
    # Per-process pool sizes. Keep api_instances * api + worker + cli below the Supabase pooler limit.
    database_pool_max_size: int = 10
    worker_database_pool_max_size: int = 4
    database_statement_timeout_ms: int = 15_000
    database_idle_in_transaction_timeout_ms: int = 30_000
    supabase_url: str = ""
    supabase_service_role_key: SecretStr = SecretStr("")
    evidence_bucket: str = "incident-evidence"
    runbook_bucket: str = "runbooks"

    # Hindsight
    hindsight_base_url: str = "https://api.hindsight.vectorize.io"
    hindsight_api_key: SecretStr = SecretStr("")
    hindsight_bank_id: str = "pagermind-incidents"
    hindsight_webhook_secret: SecretStr = SecretStr("")
    hindsight_webhook_url: str = ""
    hindsight_webhook_tolerance_seconds: int = 300
    hindsight_include_tool_calls: bool = False

    # Groq
    groq_api_key: SecretStr = SecretStr("")
    groq_primary_model: str = "openai/gpt-oss-120b"
    groq_fallback_model: str = "qwen/qwen3-32b"

    # Langfuse
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_host: str = "https://cloud.langfuse.com"

    # Sentry
    sentry_dsn: SecretStr = SecretStr("")
    sentry_traces_sample_rate: float = 0.1

    # Memory worker
    worker_poll_interval_seconds: float = 2.0
    worker_batch_size: int = 10
    worker_reconcile_after_seconds: int = 120
    worker_base_backoff_seconds: int = 10
    worker_heartbeat_stale_seconds: int = 90
    health_snapshot_interval_seconds: int = 300

    # Retention (operational telemetry only; incidents, conversations, feedback and audit are kept).
    # Webhook claims must outlive Hindsight's redelivery window (about seven hours).
    retention_webhook_days: int = 14
    retention_health_snapshot_days: int = 30
    retention_guardrail_result_days: int = 90

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key.get_secret_value())

    @property
    def sentry_enabled(self) -> bool:
        return bool(self.sentry_dsn.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
