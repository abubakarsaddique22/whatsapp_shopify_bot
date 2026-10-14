"""FakeLLM: prepared answers, prepared failures, call log (Phase 5, step 3)."""

import pytest

from app.core.errors import ProviderError
from app.integrations.llm.base import ChatModel
from app.integrations.llm.fake_llm import FakeLLM


async def test_returns_the_prepared_answers_in_order():
    llm = FakeLLM(["first", "second"])
    assert await llm.complete("sys", "hello") == "first"
    assert await llm.complete("sys", "again") == "second"


async def test_remembers_every_call():
    llm = FakeLLM(["a", "b"])
    await llm.complete("system 1", "user 1")
    await llm.complete("system 2", "user 2")
    assert llm.calls == [("system 1", "user 1"), ("system 2", "user 2")]


async def test_prepared_exception_is_raised():
    llm = FakeLLM([ProviderError("timeout"), "ok after failure"])
    with pytest.raises(ProviderError, match="timeout"):
        await llm.complete("sys", "user")
    assert await llm.complete("sys", "user") == "ok after failure"


async def test_one_call_too_many_fails_loudly():
    llm = FakeLLM(["only one"])
    await llm.complete("sys", "user")
    with pytest.raises(ProviderError, match="no more prepared answers"):
        await llm.complete("sys", "user")
    assert len(llm.calls) == 2  # the extra call is still logged


async def test_empty_fake_has_no_answers():
    with pytest.raises(ProviderError):
        await FakeLLM([]).complete("sys", "user")


def test_the_original_list_is_not_changed():
    answers = ["a", "b"]
    llm = FakeLLM(answers)
    assert llm._answers is not answers


def test_fake_can_be_used_where_a_chat_model_is_expected():
    llm: ChatModel = FakeLLM(["x"])
    assert llm is not None
