"""Application settings, loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every field maps 1:1 to an env var."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # Application
    app_name: str = "GumbInvest"
    # Set by the tray launcher (never by Docker): serve the built SPA from
    # FastAPI and assume the in-process scheduler instead of Celery.
    desktop_mode: bool = False
    log_level: str = "INFO"
    base_currency: str = "BRL"
    timezone: str = "America/Sao_Paulo"
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    # Infrastructure
    database_url: str = "postgresql+psycopg2://gumbinvest:gumbinvest@db:5432/gumbinvest"
    redis_url: str = "redis://redis:6379/0"

    # Market data
    market_data_provider: str = "yahoo"
    brapi_token: str = ""
    brapi_base_url: str = "https://brapi.dev/api"
    quote_cache_ttl: int = 900
    price_refresh_minutes: int = 30
    snapshot_time: str = "23:10"

    # Backups
    backup_time: str = "03:30"
    backup_dir: str = "/backups"
    backup_keep: int = 14

    # Importer
    auto_import_dir: str = "/data"
    auto_import_on_startup: bool = True

    # First-run downloads (PTAX, index series, Ibovespa, Tesouro) at startup.
    # The test suite switches this off: every `with TestClient(app)` runs the
    # lifespan, and hundreds of app starts against live BCB/Yahoo made the
    # suite slow and rate-limit flaky. The half-hourly heal job covers a real
    # install that boots with this off or with the network down.
    bootstrap_market_data: bool = True

    # AI assistant (asset chat). Provider chosen in the UI (or AI_PROVIDER);
    # a missing key disables the endpoint gracefully. Keys can come from the
    # env or be saved per-instance through Configurações (app/services/secrets).
    ai_provider: str = "anthropic"
    ai_model: str = ""  # empty = the chosen provider's default model
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    grok_api_key: str = ""
    groq_api_key: str = ""

    # SEC EDGAR requires a User-Agent that names the client and a contact —
    # requests without one are answered 403. Put your real e-mail here.
    sec_user_agent: str = "GumbInvest/1.0 (self-hosted; contact: admin@example.com)"

    request_timeout: float = Field(default=20.0, description="HTTP timeout for market data calls")

    @field_validator("market_data_provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        return value.strip().lower() or "none"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()


settings = get_settings()
