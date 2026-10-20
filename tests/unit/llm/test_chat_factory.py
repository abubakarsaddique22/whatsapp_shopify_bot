"""chat_factory: which model the settings give you, and how the Groq wrapper fails
(Phase 5, step 3). No network: the real model is replaced by a stub."""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.core.errors import ConfigError, ProviderError
from app.core.settings import Settings
from app.integrations.llm.chat_factory import (
    GROQ_BASE_URL,
    OpenAICompatChat,
    get_chat_model,
    start_token_count,
)
from app.integrations.llm.fake_llm import FakeLLM

KEY = "gsk_test_secret_123"


def make_settings(**overrides):
    """Settings that ignore .env and the machine's environment for the LLM fields."""
    values = {
        "llm_provider": "",
        "router_model": "",
        "composer_model": "",
        "llm_api_key": SecretStr(""),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ---- get_chat_model --------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["", "   ", "openai", "gpt"])
def test_empty_or_unknown_provider_is_a_config_error(provider):
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        get_chat_model("router", make_settings(llm_provider=provider))


def test_fake_provider_gives_an_empty_fake():
    llm = get_chat_model("router", make_settings(llm_provider="fake"))
    assert isinstance(llm, FakeLLM)


def test_groq_gives_the_real_wrapper():
    settings = make_settings(
        llm_provider=" Groq ", router_model="some-model", llm_api_key=SecretStr(KEY)
    )
    assert isinstance(get_chat_model("router", settings), OpenAICompatChat)


def test_groq_without_key_is_a_config_error():
    settings = make_settings(llm_provider="groq", router_model="some-model")
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        get_chat_model("router", settings)


def test_groq_without_model_is_a_config_error():
    settings = make_settings(llm_provider="groq", llm_api_key=SecretStr(KEY))
    with pytest.raises(ConfigError, match="router model"):
        get_chat_model("router", settings)


def test_composer_needs_its_own_model_name():
    settings = make_settings(
        llm_provider="groq", router_model="router-model", llm_api_key=SecretStr(KEY)
    )
    assert isinstance(get_chat_model("router", settings), OpenAICompatChat)
    with pytest.raises(ConfigError, match="composer model"):
        get_chat_model("composer", settings)


def test_the_config_error_does_not_contain_the_key():
    settings = make_settings(llm_provider="groq", llm_api_key=SecretStr(KEY))
    with pytest.raises(ConfigError) as info:
        get_chat_model("router", settings)
    assert KEY not in str(info.value)


def test_groq_url_is_the_openai_compatible_one():
    assert GROQ_BASE_URL.startswith("https://") and GROQ_BASE_URL.endswith("/openai/v1")


# ---- OpenAICompatChat.complete (the real model replaced by a stub) ---------------------


class StubLLM:
    def __init__(self, content=None, error=None, usage=None):
        self.content = content
        self.error = error
        self.usage = usage
        self.messages = None

    async def ainvoke(self, messages):
        self.messages = messages
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.content, usage_metadata=self.usage)


def make_chat(stub):
    chat = OpenAICompatChat("some-model", SecretStr(KEY), "http://localhost:1/v1", timeout=5)
    chat._llm = stub  # type: ignore
    return chat


async def test_complete_returns_the_text_and_sends_system_and_user():
    stub = StubLLM(content="hello customer")
    chat = make_chat(stub)
    assert await chat.complete("be polite", "price?") == "hello customer"
    assert stub.messages == [("system", "be polite"), ("human", "price?")]


@pytest.mark.parametrize("content", ["", "   ", None, ["a", "list"]])
async def test_empty_or_non_text_answer_is_a_provider_error(content):
    with pytest.raises(ProviderError, match="no text"):
        await make_chat(StubLLM(content=content)).complete("sys", "user")


async def test_provider_failure_becomes_provider_error_without_the_key():
    class FakeHttpError(Exception):
        status_code = 401

    error = FakeHttpError(f"Incorrect API key provided: {KEY}")
    with pytest.raises(ProviderError) as info:
        await make_chat(StubLLM(error=error)).complete("sys", "user")

    exc = info.value
    assert exc.detail == {"error_type": "FakeHttpError", "status_code": 401}
    assert KEY not in str(exc) and KEY not in str(exc.detail)
    assert exc.__cause__ is None  # the original message (with the key) is not chained
    assert exc.__suppress_context__


async def test_the_tokens_of_a_turn_are_counted_for_the_log_line():
    counts = start_token_count()
    chat = make_chat(StubLLM(content="ok", usage={"input_tokens": 1200, "output_tokens": 80}))
    await chat.complete("sys", "user")
    await chat.complete("sys", "user")
    assert counts == {"llm_calls": 2, "tokens_in": 2400, "tokens_out": 160}


async def test_a_provider_without_usage_still_counts_the_call():
    counts = start_token_count()
    await make_chat(StubLLM(content="ok")).complete("sys", "user")
    assert counts == {"llm_calls": 1, "tokens_in": 0, "tokens_out": 0}


async def test_failure_without_status_code():
    with pytest.raises(ProviderError) as info:
        await make_chat(StubLLM(error=TimeoutError())).complete("sys", "user")
    assert info.value.detail == {"error_type": "TimeoutError", "status_code": None}
