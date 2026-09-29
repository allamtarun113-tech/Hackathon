"""Settings from environment variables (or a local .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Config:
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    groq_fallback_model: str = os.getenv("GROQ_FALLBACK_MODEL", "qwen/qwen3-32b")
    hindsight_base_url: str = os.getenv("HINDSIGHT_BASE_URL", "https://api.hindsight.vectorize.io")
    hindsight_api_key: str = os.getenv("HINDSIGHT_API_KEY", "")
    bank_id: str = os.getenv("HINDSIGHT_BANK_ID", "pagermind-incidents")
    data_dir: Path = ROOT / "data"


def get_config() -> Config:
    return Config()
