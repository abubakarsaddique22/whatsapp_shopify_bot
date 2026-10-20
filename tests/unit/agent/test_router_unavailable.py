"""Phase 12 failure drill llm_down: a REAL provider outage is reported by the router.

The real chat model raises ProviderUnavailable; route_intent_checked says provider_down=True so
the graph can tell the customer honestly. A plain ProviderError (a FakeLLM that ran out of
answers, the convention of many tests) keeps the old 'unclear' behaviour.
"""

import json
from typing import Any

import pytest

from app.agent.router import route_intent, route_intent_checked
from app.core.errors import ProviderError, ProviderUnavailable
from app.domain.models import Intent
from app.integrations.llm.chat_factory import OpenAICompatChat
from app.integrations.llm.fake_llm import FakeLLM


async def test_a_provider_outage_is_reported() -> None:
    llm = FakeLLM([ProviderUnavailable("llm call failed")])

    out, provider_down = await route_intent_checked("iski price kya hai?", llm)

    assert provider_down is True
    assert out.intent is Intent.UNCLEAR
    assert len(llm.calls) == 1  # no repair call: the provider is down


async def test_a_plain_provider_error_is_still_just_unclear() -> None:
    out, provider_down = await route_intent_checked("iski price kya hai?", FakeLLM([]))

    assert provider_down is False
    assert out.intent is Intent.UNCLEAR


async def test_a_good_answer_is_not_down() -> None:
    answer = json.dumps({"language": "roman_ur", "intent": "price", "confidence": 0.9})

    out, provider_down = await route_intent_checked("iski price kya hai?", FakeLLM([answer]))

    assert provider_down is False
    assert out.intent is Intent.PRICE


async def test_route_intent_keeps_its_old_shape() -> None:
    out = await route_intent("iski price kya hai?", FakeLLM([ProviderUnavailable("down")]))

    assert out.intent is Intent.UNCLEAR  # evals and the debug reply see a RouterOutput, as before


class _Boom:
    async def ainvoke(self, messages: Any) -> Any:
        raise TimeoutError("network down")


class _Empty:
    async def ainvoke(self, messages: Any) -> Any:
        class Answer:
            content = "  "

        return Answer()


@pytest.mark.parametrize("inner", [_Boom(), _Empty()])
async def test_the_real_chat_model_raises_provider_unavailable(inner: Any) -> None:
    chat = OpenAICompatChat.__new__(OpenAICompatChat)  # no network client is built
    chat._llm = inner

    with pytest.raises(ProviderUnavailable) as caught:
        await chat.complete("system", "user")

    assert isinstance(caught.value, ProviderError)  # every old `except ProviderError` still works
