"""Groq speech adapter (Phase 6, step 5a). No network and no real key: httpx.MockTransport plays
Groq. The answers below have the shape of Groq's verbose_json (see the module text of groq_stt)."""

import math
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.errors import ConfigError, TranscriptionError
from app.core.settings import Settings
from app.integrations.speech import groq_stt
from app.integrations.speech.factory import get_stt_provider
from app.integrations.speech.groq_stt import GroqSTT, parse_response

KEY = "gsk_secret_value"
MODEL = "whisper-large-v3-turbo"


def segment(
    text: str, start: float, end: float, logprob: float = -0.1, no_speech: float = 0.01
) -> dict[str, Any]:
    return {
        "text": f" {text}",
        "start": start,
        "end": end,
        "avg_logprob": logprob,
        "no_speech_prob": no_speech,
    }


def answer(segments: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    text = "".join(s["text"] for s in segments)
    return {"text": text, "language": "urdu", "duration": 4.0, "segments": segments, **extra}


class FakeGroq:
    """Records every request and answers with the prepared response."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def groq(monkeypatch: pytest.MonkeyPatch, response: httpx.Response | Exception) -> FakeGroq:
    server = FakeGroq(response)
    transport = httpx.MockTransport(server.handler)
    monkeypatch.setattr(groq_stt, "_make_client", lambda t: httpx.AsyncClient(transport=transport))
    return server


@pytest.fixture
def wav(tmp_path: Path) -> Path:
    path = tmp_path / "note.wav"
    path.write_bytes(b"RIFF-not-really-audio")
    return path


def ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


# ---- the request


async def test_the_request_has_the_key_the_model_and_the_file(
    monkeypatch: pytest.MonkeyPatch, wav: Path
) -> None:
    server = groq(monkeypatch, ok(answer([segment("iski price kya hai", 0.0, 3.0)])))
    await GroqSTT(KEY, MODEL).transcribe(wav)

    request, body = server.requests[0], server.bodies[0]
    assert str(request.url) == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert MODEL.encode() in body
    assert b"verbose_json" in body
    assert b'name="file"' in body
    assert b"RIFF-not-really-audio" in body
    assert b'name="language"' not in body  # no hint, no language field


async def test_the_language_hint_is_sent(monkeypatch: pytest.MonkeyPatch, wav: Path) -> None:
    server = groq(monkeypatch, ok(answer([segment("hello", 0.0, 1.0)])))
    await GroqSTT(KEY, MODEL).transcribe(wav, language_hint="ur")
    assert b'name="language"' in server.bodies[0]
    assert b"\r\n\r\nur\r\n" in server.bodies[0]


# ---- the answer


async def test_a_normal_answer_becomes_a_result(monkeypatch: pytest.MonkeyPatch, wav: Path) -> None:
    segments = [segment("iski price", 0.0, 1.5), segment("kya hai", 1.5, 3.0)]
    groq(monkeypatch, ok(answer(segments)))

    result = await GroqSTT(KEY, MODEL).transcribe(wav)

    assert result.text == "iski price kya hai"
    assert result.language == "urdu"  # the gate reads language names
    assert result.duration_s == 4.0
    assert (result.provider, result.model) == ("groq", MODEL)
    assert result.latency_ms >= 0
    assert result.confidence == pytest.approx(math.exp(-0.1))


def test_confidence_is_weighted_by_the_length_of_each_segment() -> None:
    segments = [segment("a", 0.0, 1.0, logprob=-0.1), segment("b", 1.0, 4.0, logprob=-0.5)]
    result = parse_response(answer(segments), model=MODEL, latency_ms=5)
    assert result.confidence == pytest.approx(math.exp((-0.1 * 1 + -0.5 * 3) / 4))


def test_a_silent_segment_is_dropped() -> None:
    """Whisper's own rule: no_speech_prob above 0.6 and avg_logprob below -1.0 is silence."""
    segments = [
        segment("iski price kya hai", 0.0, 3.0),
        segment("thank you for watching", 3.0, 5.0, logprob=-1.5, no_speech=0.9),
    ]
    result = parse_response(answer(segments), model=MODEL, latency_ms=5)
    assert result.text == "iski price kya hai"
    assert result.confidence == pytest.approx(math.exp(-0.1))


def test_a_doubtful_but_spoken_segment_is_kept() -> None:
    """Low logprob alone is not silence: it stays and lowers the confidence."""
    segments = [segment("kuch samajh nahi aaya", 0.0, 3.0, logprob=-1.5, no_speech=0.1)]
    result = parse_response(answer(segments), model=MODEL, latency_ms=5)
    assert result.text == "kuch samajh nahi aaya"
    assert result.confidence == pytest.approx(math.exp(-1.5))


def test_only_silence_gives_an_empty_text_and_no_confidence() -> None:
    segments = [segment("thank you", 0.0, 2.0, logprob=-1.5, no_speech=0.95)]
    result = parse_response(answer(segments), model=MODEL, latency_ms=5)
    assert result.text == ""
    assert result.confidence is None


def test_without_segments_the_plain_text_is_used_and_confidence_is_unknown() -> None:
    result = parse_response({"text": " hello there "}, model=MODEL, latency_ms=5)
    assert result.text == "hello there"
    assert result.confidence is None
    assert result.language is None
    assert result.duration_s == 0.0  # the pipeline fills it in from the audio


def test_a_confidence_above_one_is_kept_inside_the_range() -> None:
    result = parse_response(
        answer([segment("a b", 0.0, 1.0, logprob=0.5)]), model=MODEL, latency_ms=5
    )
    assert result.confidence == 1.0


# ---- errors: no key, no path, no audio in any message


@pytest.mark.parametrize("status", [400, 401, 429, 500])
async def test_http_errors_become_transcription_errors(
    monkeypatch: pytest.MonkeyPatch, wav: Path, status: int
) -> None:
    groq(monkeypatch, httpx.Response(status, json={"error": {"message": f"echo {KEY}"}}))
    with pytest.raises(TranscriptionError) as caught:
        await GroqSTT(KEY, MODEL).transcribe(wav)
    assert str(status) in str(caught.value)
    assert KEY not in str(caught.value)
    assert str(wav) not in str(caught.value)
    assert caught.value.detail == {"status": status}


async def test_a_timeout_becomes_a_transcription_error(
    monkeypatch: pytest.MonkeyPatch, wav: Path
) -> None:
    groq(monkeypatch, httpx.ReadTimeout("slow"))
    with pytest.raises(TranscriptionError):
        await GroqSTT(KEY, MODEL).transcribe(wav)


NOT_A_JSON_OBJECT = [httpx.Response(200, text="not json"), httpx.Response(200, json=[1, 2])]


@pytest.mark.parametrize("response", NOT_A_JSON_OBJECT)
async def test_an_answer_that_is_not_a_json_object_is_an_error(
    monkeypatch: pytest.MonkeyPatch, wav: Path, response: httpx.Response
) -> None:
    groq(monkeypatch, response)
    with pytest.raises(TranscriptionError):
        await GroqSTT(KEY, MODEL).transcribe(wav)


# ---- the factory


def test_the_factory_builds_groq_from_the_settings() -> None:
    settings = Settings(
        stt_provider="groq", stt_api_key=SecretStr(KEY), stt_model="whisper-large-v3"
    )
    stt = get_stt_provider(settings)
    assert isinstance(stt, GroqSTT)
    assert stt.model == "whisper-large-v3"


def test_the_factory_needs_a_key_for_groq() -> None:
    with pytest.raises(ConfigError):
        get_stt_provider(Settings(stt_provider="groq", stt_api_key=SecretStr("")))


def test_the_factory_needs_a_model_for_groq() -> None:
    settings = Settings(stt_provider="groq", stt_api_key=SecretStr(KEY), stt_model="")
    with pytest.raises(ConfigError):
        get_stt_provider(settings)
