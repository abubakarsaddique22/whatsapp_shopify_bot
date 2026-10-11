"""Typed settings read from environment variables / .env.

Secrets use SecretStr so they print as '**********'. Never log .get_secret_value().
"""

from functools import lru_cache

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # app
    app_env: str = "local"  # local | staging | production
    log_level: str = "INFO"
    kill_switch: bool = False
    admin_api_key: SecretStr = SecretStr("")
    admin_endpoints_enabled: bool = False
    phone_hash_pepper: SecretStr = SecretStr("change-me-random")

    # database
    database_url: str = "postgresql+asyncpg://bot:bot@localhost:5432/bot"

    # shopify (read-only)
    shopify_shop_domain: str = ""
    shopify_api_version: str = "2026-10"
    shopify_client_id: str = ""
    shopify_client_secret: SecretStr = SecretStr("")
    shopify_auth_mode: str = "client_credentials"
    shopify_offline_token: SecretStr = SecretStr("")
    low_stock_threshold: int = 3
    show_exact_stock: bool = False
    live_stock_cache_seconds: int = 0

    # whatsapp
    meta_graph_version: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_verify_token: SecretStr = SecretStr("change-me-random")
    max_audio_seconds: int = 90
    max_audio_bytes: int = 5_000_000

    # llm
    llm_provider: str = ""
    router_model: str = ""
    composer_model: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_timeout_seconds: int = 20

    # speech
    stt_provider: str = "fake"
    stt_api_key: SecretStr = SecretStr("")
    stt_min_confidence: float = 0.0
    stt_min_words: int = 2

    # embeddings / rag
    embedding_provider: str = "fake"
    embedding_model: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    rag_top_k: int = 6
    rag_min_score: float = 0.0

    # language
    urdu_script_replies: bool = False
    language_sticky_messages: int = 5

    # rate limits
    rate_limit_per_minute: int = 8
    rate_limit_per_hour: int = 60
    rate_limit_per_day: int = 200

    # handoff
    handoff_channel: str = "email"
    handoff_target: str = ""
    handoff_pause_hours: int = 12
    business_hours: str = "10:00-20:00"

    # observability
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "wa-shopify-bot-dev"

    @model_validator(mode="after")
    def _production_must_be_complete(self) -> "Settings":
        """Fail fast in production if a required secret is missing or still a placeholder."""
        if self.app_env != "production":
            return self
        required = {
            "SHOPIFY_SHOP_DOMAIN": self.shopify_shop_domain,
            "WHATSAPP_APP_SECRET": self.whatsapp_app_secret.get_secret_value(),
            "WHATSAPP_ACCESS_TOKEN": self.whatsapp_access_token.get_secret_value(),
            "WHATSAPP_VERIFY_TOKEN": self.whatsapp_verify_token.get_secret_value(),
            "PHONE_HASH_PEPPER": self.phone_hash_pepper.get_secret_value(),
        }
        missing = [k for k, v in required.items() if not v or v.startswith("change-me")]
        if missing:
            raise ValueError(f"Missing or placeholder settings in production: {missing}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
