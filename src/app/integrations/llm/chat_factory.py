"""Phase 5, step 3: build the chat model named in the settings.

    llm = get_chat_model("router")       # or "composer"
    text = await llm.complete(system, user)

LLM_PROVIDER decides what you get:
- "groq"  a real model through Groq's OpenAI-compatible API (needs LLM_API_KEY and the model
          name in ROUTER_MODEL / COMPOSER_MODEL)
- "fake"  an empty FakeLLM (no answers; tests normally build their own FakeLLM directly)
- empty or anything else: ConfigError. On purpose: a missing setting must never silently turn
  into a fake model in production.

Ollama is used only for the eval: see get_eval_chat_model() at the bottom (OLLAMA_BASE_URL).
"""

from typing import Literal

from pydantic import SecretStr

from app.core.errors import ConfigError, ProviderUnavailable
from app.core.settings import Settings, get_settings
from app.integrations.llm.base import ChatModel
from app.integrations.llm.fake_llm import FakeLLM

GROQ_BASE_URL = "https://api.groq.com/openai/v1"  # VERIFY against Groq's current docs

Role = Literal["router", "composer"]


class OpenAICompatChat:
    """Any server that speaks the OpenAI chat API (Groq now, maybe Ollama later)."""

    def __init__(
        self, model: str, api_key: SecretStr, base_url: str, timeout: int, max_retries: int = 1
    ) -> None:
        from langchain_openai import ChatOpenAI  # imported here: fake-LLM tests skip the cost

        self._llm = ChatOpenAI(  # type: ignore
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,  # the OpenAI client waits and retries on 429 by itself
            temperature=0,  # same message, same routing, as far as the model allows
        )

    async def complete(self, system: str, user: str) -> str:
        try:
            response = await self._llm.ainvoke([("system", system), ("human", user)])
        except Exception as exc:
            # Do not copy str(exc) into the error: provider messages can echo part of the key.
            raise ProviderUnavailable(
                "llm call failed",
                detail={
                    "error_type": type(exc).__name__,
                    "status_code": getattr(exc, "status_code", None),
                },
            ) from None
        text = response.content
        if not isinstance(text, str) or not text.strip():
            raise ProviderUnavailable("llm returned no text")
        return text


def get_chat_model(role: Role = "router", settings: Settings | None = None) -> ChatModel:
    settings = settings or get_settings()
    provider = settings.llm_provider.strip().lower()

    if provider == "fake":
        return FakeLLM([])

    if provider == "groq":
        model = settings.router_model if role == "router" else settings.composer_model
        if not model or not settings.llm_api_key.get_secret_value():
            raise ConfigError(f"LLM_PROVIDER=groq needs LLM_API_KEY and the {role} model name")
        return OpenAICompatChat(
            model=model,
            api_key=settings.llm_api_key,
            base_url=GROQ_BASE_URL,
            timeout=settings.llm_timeout_seconds,
        )

    raise ConfigError(f"LLM_PROVIDER must be 'groq' or 'fake', got '{provider}'")


def get_eval_chat_model(settings: Settings | None = None) -> ChatModel:
    """The model `eval-router` uses: Ollama (local or cloud), whatever LLM_PROVIDER says.

    The app keeps using Groq; only the eval goes through Ollama, so a Groq 429 can no longer
    spoil the baseline. More retries and a longer timeout than the app, because the eval is
    a batch job and nobody is waiting on a WhatsApp reply.
    """
    settings = settings or get_settings()
    if not settings.eval_router_model:
        raise ConfigError("eval-router needs EVAL_ROUTER_MODEL (for example gpt-oss:120b-cloud)")
    return OpenAICompatChat(
        model=settings.eval_router_model,
        api_key=settings.ollama_api_key,
        base_url=settings.ollama_base_url,
        timeout=max(settings.llm_timeout_seconds, 60),
        max_retries=5,
    )
