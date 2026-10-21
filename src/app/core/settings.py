"""Typed settings read from environment variables / .env.

Secrets use SecretStr so they print as '**********'. Never log .get_secret_value().
"""

import re
from functools import lru_cache

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 'log' only prints the notification: for local development, no email or Slack needed
HANDOFF_CHANNELS = {"email", "slack", "whatsapp_staff", "log"}
_BUSINESS_HOURS = re.compile(r"([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d")
_UTC_OFFSET = re.compile(r"[+-](0\d|1[0-4]):[0-5]\d")


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
    # the router's answer to the SAME message in the same situation is reused this long (agent/
    # router.py). 0 = off. Useful during ads (one pre-filled text from many customers).
    router_cache_seconds: int = 0

    # whatsapp
    meta_graph_version: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_verify_token: SecretStr = SecretStr("change-me-random")
    max_audio_seconds: int = 90
    max_audio_bytes: int = 5_000_000
    # Embedded Signup page (/signup, api/signup.py): connects the store's WhatsApp Business app
    # number to the bot (Coexistence). META_APP_ID = the number at the top of the App Dashboard;
    # SIGNUP_CONFIG_ID = Facebook Login for Business > Configurations. SIGNUP_KEY = 24+ random
    # characters, part of the page link; empty = the page does not exist (404).
    meta_app_id: str = ""
    signup_config_id: str = ""
    signup_key: SecretStr = SecretStr("")

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
    # Languages the voice gate accepts, comma separated (codes: en, ur, pa, hi ...). Whisper often
    # labels Pakistani Urdu as Hindi, so 'hi' may be needed; the bake-off decides.
    stt_allowed_languages: str = "en,ur,pa"
    # Language hint sent to Whisper (for example ur). Empty = Whisper detects the language itself.
    # A fixed hint makes English voice notes worse, so keep it empty unless the bake-off says so.
    stt_language_hint: str = ""
    # When Whisper (no hint) names a language not in STT_ALLOWED_LANGUAGES, transcribe once more
    # with this language (for example ur). Empty = off. Voice pilot 2026-10-20: Punjabi came back
    # as 'Tagalog' and accented English as 'Bengali' without a hint.
    stt_retry_language: str = ""
    # A short text sent to Whisper as its `prompt`: it nudges the spelling (Latin script instead of
    # Devanagari, product names like GA-2100). Write it like the customers talk, for example:
    # "GA-2100 ki price kya hai? Black wali watch available hai? Rolex, Casio, G-Shock."
    # Empty = no prompt. Keep it under about 200 words.
    stt_prompt: str = ""
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
    # daily LLM + speech spend in your currency (Phase 11): alarm at 80 %, 'handoff only' at 100 %.
    # 0 = no budget set, the check is off.
    daily_cost_budget: float = 0.0
    # What one customer message costs you on average (LLM calls, and for a voice note also the
    # speech-to-text), in the same currency. The budget check multiplies these by the messages
    # of the last 24 hours: an estimate, not a bill. 0 = free (local model, free tier).
    cost_per_text_message: float = 0.0
    cost_per_voice_message: float = 0.0

    # retention (Phase 11, step 9): how many days data is kept. `python -m app.jobs.retention`
    # deletes what is older. A customer who wrote nothing for retention_conversation_days is erased
    # completely, phone number included.
    retention_conversation_days: int = 180
    retention_message_days: int = 90  # transcript lines (messages table)
    retention_queue_days: int = 30  # finished inbox and outbox rows
    retention_handoff_days: int = 90  # resolved handoffs
    retention_security_days: int = 365  # security_events (hashes and short facts only)
    retention_sync_days: int = 14  # finished Shopify sync events (failed ones: 90 days at least)
    retention_checkpoint_days: int = 7  # LangGraph step history of idle conversations

    # handoff
    handoff_channel: str = "email"
    handoff_target: str = ""
    handoff_pause_hours: int = 12
    business_hours: str = "10:00-20:00"  # local time, HH:MM-HH:MM
    # the time zone of business_hours as an offset from UTC (Pakistan has no summer time: +05:00)
    business_utc_offset: str = "+05:00"
    # Bot ON / OFF button for the team (owner/presence.py, api/team.py). Needs WhatsApp
    # Coexistence for the team's replies: they answer from the WhatsApp Business app on the SAME
    # number and Meta sends those replies to the webhook (smb_message_echoes).
    # Bot OFF: a new message waits this long; nobody answered -> the bot answers and turns ON.
    team_wait_minutes: int = 3
    # password of the /team page (one for the whole team, 8+ characters). Empty = no page.
    team_password: SecretStr = SecretStr("")
    # HANDOFF_CHANNEL=email only (handoff/notifier.py): the mail server that sends the notification.
    # Port 465 = SSL from the start, any other port (587) = STARTTLS. Empty SMTP_FROM = SMTP_USER.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""

    # observability
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_project: str = "wa-shopify-bot-dev"
    # Phase 12 Step 7: traces of the live bot hold the customer's message and the bot's reply.
    # true = LangSmith keeps the steps, timings and tokens but not the text (required in
    # production when tracing is on). Eval datasets and experiments are masked separately.
    langsmith_hide_text: bool = False

    @property
    def stt_allowed_language_set(self) -> frozenset[str]:
        return frozenset(
            code.strip().lower() for code in self.stt_allowed_languages.split(",") if code.strip()
        )

    @model_validator(mode="after")
    def _phase11_values_are_valid(self) -> "Settings":
        """Fail fast on a typo in the Phase 11 settings, in every environment."""
        if self.handoff_channel not in HANDOFF_CHANNELS:
            raise ValueError(f"HANDOFF_CHANNEL must be one of {sorted(HANDOFF_CHANNELS)}")
        if not _BUSINESS_HOURS.fullmatch(self.business_hours):
            raise ValueError("BUSINESS_HOURS must look like 10:00-20:00")
        if not _UTC_OFFSET.fullmatch(self.business_utc_offset):
            raise ValueError("BUSINESS_UTC_OFFSET must look like +05:00")
        if self.daily_cost_budget < 0:
            raise ValueError("DAILY_COST_BUDGET cannot be negative")
        if self.cost_per_text_message < 0 or self.cost_per_voice_message < 0:
            raise ValueError("COST_PER_TEXT_MESSAGE and COST_PER_VOICE_MESSAGE cannot be negative")
        for name in ("rate_limit_per_minute", "rate_limit_per_hour", "rate_limit_per_day"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name.upper()} must be at least 1")
        if self.handoff_pause_hours < 1:
            raise ValueError("HANDOFF_PAUSE_HOURS must be at least 1")
        if not 0 <= self.router_cache_seconds <= 86400:
            raise ValueError("ROUTER_CACHE_SECONDS must be between 0 and 86400")
        if not 1 <= self.team_wait_minutes <= 60:
            raise ValueError("TEAM_WAIT_MINUTES must be between 1 and 60")
        if 0 < len(self.team_password.get_secret_value()) < 8:
            raise ValueError("TEAM_PASSWORD must be empty (no /team page) or 8+ characters")
        if 0 < len(self.signup_key.get_secret_value()) < 24:
            raise ValueError("SIGNUP_KEY must be empty (no /signup page) or 24+ characters")
        for name in (
            "retention_conversation_days",
            "retention_message_days",
            "retention_queue_days",
            "retention_handoff_days",
            "retention_security_days",
            "retention_sync_days",
            "retention_checkpoint_days",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name.upper()} must be at least 1")
        # admin endpoints can pause the bot for everybody: never open them with a weak key
        key = self.admin_api_key.get_secret_value()
        if self.admin_endpoints_enabled and (len(key) < 24 or key.startswith("change-me")):
            raise ValueError("ADMIN_ENDPOINTS_ENABLED needs an ADMIN_API_KEY of 24+ characters")
        return self

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
        if self.langsmith_tracing and not self.langsmith_hide_text:
            # a trace would carry customer messages to a third party (Blueprint 9, privacy)
            raise ValueError("LANGSMITH_TRACING in production needs LANGSMITH_HIDE_TEXT=true")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
