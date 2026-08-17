# backend/app/core/config.py
from pathlib import Path
from pydantic_settings import BaseSettings


# Root dự án: .../05.API_AI
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    # Tắt SQL mặc định
    ENABLE_SQL: str = "no"

    # OpenAI
    OPENAI_API_KEY: str | None = None
    OPENAI_MODEL: str | None = None

    # Gemini
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str | None = None

    # OpenRouter
    OPENROUTER_API_KEY: str | None = None
    OPENROUTER_MODEL: str | None = None

    # LLM provider selector: openai | gemini | openrouter
    LLM_PROVIDER: str | None = None

    class Config:
        env_file = str(ENV_PATH)
        extra = "ignore"


settings = Settings()


def is_sql_enabled() -> bool:
    return str(settings.ENABLE_SQL or "no").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )