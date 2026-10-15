"""FakeSTT and the speech factory (Phase 6, step 1). No network, no key, no real audio."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.errors import ConfigError, TranscriptionError
from app.core.settings import Settings
from app.integrations.speech.base import TranscriptionResult
from app.integrations.speech.factory import get_stt_provider
from app.integrations.speech.fake_stt import FakeSTT, fake_result

WAV = Path("does-not-exist.wav")  # the fake never opens the file


# ---- FakeSTT


async def test_prepared_strings_come_back_in_order() -> None:
    stt = FakeSTT(["iski price kya hai", "how much is it"])
    first = await stt.transcribe(WAV)
    second = await stt.transcribe(WAV, language_hint="en")
    assert (first.text, second.text) == ("iski price kya hai", "how much is it")
    assert first.provider == "fake"
    assert stt.calls == [(WAV, None), (WAV, "en")]


async def test_a_prepared_result_is_returned_as_it_is() -> None:
    result = fake_result("hello", confidence=0.2, duration_s=1.5, language="en")
    out = await FakeSTT([result]).transcribe(WAV)
    assert out is result
    assert out.confidence == 0.2


async def test_a_prepared_exception_is_raised() -> None:
    stt = FakeSTT([TranscriptionError("boom")])
    with pytest.raises(TranscriptionError):
        await stt.transcribe(WAV)


async def test_running_out_of_answers_fails_loudly() -> None:
    stt = FakeSTT(["one"])
    await stt.transcribe(WAV)
    with pytest.raises(TranscriptionError):
        await stt.transcribe(WAV)


async def test_default_text_answers_every_further_call() -> None:
    stt = FakeSTT(default_text="iski price kya hai")
    assert (await stt.transcribe(WAV)).text == "iski price kya hai"
    assert (await stt.transcribe(WAV)).text == "iski price kya hai"


# ---- TranscriptionResult


def test_result_confidence_must_be_between_0_and_1() -> None:
    with pytest.raises(ValidationError):
        fake_result("hello", confidence=1.5)


def test_result_confidence_may_be_missing() -> None:
    result = TranscriptionResult(text="hello", provider="x", model="y")
    assert result.confidence is None
    assert result.duration_s == 0.0


# ---- factory


async def test_factory_fake_with_text() -> None:
    stt = get_stt_provider(Settings(stt_provider="fake", stt_fake_text="colour kon sa hai"))
    assert (await stt.transcribe(WAV)).text == "colour kon sa hai"


async def test_factory_fake_without_text_has_no_answers() -> None:
    stt = get_stt_provider(Settings(stt_provider="fake", stt_fake_text=""))
    with pytest.raises(TranscriptionError):
        await stt.transcribe(WAV)


@pytest.mark.parametrize("provider", ["", "nonsense"])
def test_factory_rejects_empty_and_unknown_providers(provider: str) -> None:
    with pytest.raises(ConfigError):
        get_stt_provider(Settings(stt_provider=provider))


def test_factory_real_providers_are_not_written_yet() -> None:
    with pytest.raises(ConfigError):
        get_stt_provider(Settings(stt_provider="openai"))
