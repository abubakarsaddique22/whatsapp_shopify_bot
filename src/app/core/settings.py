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
    # public https address of this server, no path (your tunnel URL in development).
    # Used by scripts/register_webhooks.py to tell Shopify where to send webhooks.
    shopify_webhook_base_url: str = ""
    # how often `python -m app.jobs.reconcile --loop` runs each job (Phase 10, step 6)
    reconcile_delta_minutes: int = 5
    reconcile_policy_minutes: int = 60
    reconcile_full_hours: int = 24
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
    # time budget of ONE customer message in the graph (Blueprint 3.7 limits): when it is used up,
    # no new composer call starts and the customer gets the safe templated reply plus a handoff.
    # A model call that is already running is still bounded by LLM_TIMEOUT_SECONDS.
    turn_budget_seconds: int = 45

    # eval only (Ollama, OpenAI-compatible): used by `eval-router`; the app keeps LLM_PROVIDER
    ollama_base_url: str = "http://localhost:11434/v1"  # cloud: https://ollama.com/v1
    ollama_api_key: SecretStr = SecretStr("ollama")  # local ignores it; cloud needs a real key
    eval_router_model: str = "gpt-oss:120b-cloud"  # same family and size as Groq's gpt-oss-120b

    # speech
    stt_provider: str = "fake"
    stt_api_key: SecretStr = SecretStr("")
    # Groq Whisper: whisper-large-v3-turbo (cheaper, faster) or whisper-large-v3 (more accurate).
    # The bake-off decides which one stays; STT_API_KEY can be the same Groq key as LLM_API_KEY.
    stt_model: str = "whisper-large-v3-turbo"
    stt_timeout_seconds: int = 30
    stt_min_confidence: float = 0.0
    stt_min_words: int = 2
    # local development only: with STT_PROVIDER=fake every voice note "says" this text
    stt_fake_text: str = ""

    # embeddings / rag: fake (tests) or sentence-transformers (local model, no key)
    embedding_provider: str = "fake"
    embedding_model: str = ""  # empty = BAAI/bge-m3
    rag_top_k: int = 6
    rag_min_score: float = 0.0

    # language
    urdu_script_replies: bool = False
    language_sticky_messages: int = 5
    # Phase 5 only: reply with what the router understood instead of 'Received'. Local use.
    debug_reply: bool = False

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
