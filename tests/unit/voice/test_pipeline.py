"""From media id to transcript (Phase 6, step 4a). No network, no ffmpeg, no database.

`prepare` is replaced by a fake that records "open" and "closed", so the tests can also check the
privacy rule: the audio is gone before the transcript is judged, and also when something fails.
"""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.core.errors import (
    AudioConversionError,
    MediaTooLarge,
    TranscriptionError,
    UnsupportedMedia,
    WhatsAppTemporaryError,
)
from app.integrations.speech.fake_stt import FakeSTT, fake_result
from app.voice.convert import PreparedAudio
from app.voice.gate import GateReason
from app.voice.pipeline import VoiceOutcome, transcribe_voice_note

WAV = Path("note.wav")
GOOD = "iski price kya hai"


def fake_prepare(
    events: list[str],
    *,
    duration_s: float = 4.0,
    size_bytes: int = 50_000,
    error: Exception | None = None,
):
    """A stand-in for prepared_audio. `error` is raised while the audio is being prepared."""

    @asynccontextmanager
    async def prepare(media_id: str, *, max_bytes: int, max_seconds: int):
        if error is not None:
            raise error
        events.append("open")
        try:
            yield PreparedAudio(
                wav_path=WAV, duration_s=duration_s, size_bytes=size_bytes, mime_type="audio/ogg"
            )
        finally:
            events.append("closed")

    return prepare


async def run(stt: FakeSTT, events: list[str], **prepare_options: object) -> VoiceOutcome:
    return await transcribe_voice_note(
        "MEDIA1",
        stt,
        max_bytes=5_000_000,
        max_seconds=90,
        min_words=2,
        min_confidence=0.5,
        prepare=fake_prepare(events, **prepare_options),  # type: ignore[arg-type]
    )


# ---- the good path


async def test_a_clear_voice_note_gives_a_transcript() -> None:
    events: list[str] = []
    stt = FakeSTT([GOOD])
    outcome = await run(stt, events)

    assert outcome.ok is True
    assert outcome.reason is None
    assert outcome.transcript is not None
    assert outcome.transcript.text == GOOD
    assert stt.calls == [(WAV, None)]
    assert events == ["open", "closed"]


async def test_the_language_hint_reaches_the_provider() -> None:
    stt = FakeSTT([GOOD])
    await transcribe_voice_note(
        "MEDIA1",
        stt,
        max_bytes=5_000_000,
        max_seconds=90,
        min_words=2,
        min_confidence=0.5,
        language_hint="ur",
        prepare=fake_prepare([]),
    )
    assert stt.calls == [(WAV, "ur")]


# ---- limits: the provider is not even called


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"duration_s": 91.0}, GateReason.TOO_LONG),
        ({"size_bytes": 5_000_001}, GateReason.TOO_LARGE),
    ],
)
async def test_too_long_or_too_big_is_refused_before_transcription(
    options: dict[str, float], reason: GateReason
) -> None:
    events: list[str] = []
    stt = FakeSTT([GOOD])
    outcome = await run(stt, events, **options)

    assert (outcome.ok, outcome.reason) == (False, reason)
    assert stt.calls == []
    assert events == ["open", "closed"]


async def test_a_file_too_big_for_the_download_asks_again() -> None:
    stt = FakeSTT([GOOD])
    outcome = await run(stt, [], error=MediaTooLarge("big"))
    assert outcome.reason == GateReason.TOO_LARGE
    assert stt.calls == []


async def test_a_damaged_file_asks_again() -> None:
    stt = FakeSTT([GOOD])
    outcome = await run(stt, [], error=AudioConversionError("ffmpeg failed"))
    assert (outcome.ok, outcome.reason) == (False, GateReason.EMPTY)
    assert stt.calls == []


# ---- a doubtful transcript is dropped (T22)


async def test_low_confidence_has_no_text_in_the_outcome() -> None:
    outcome = await run(FakeSTT([fake_result(GOOD, confidence=0.2)]), [])
    assert (outcome.ok, outcome.reason) == (False, GateReason.LOW_CONFIDENCE)
    assert outcome.transcript is None


async def test_a_repeated_phrase_is_a_hallucination() -> None:
    outcome = await run(FakeSTT(["thank you thank you thank you thank you"]), [])
    assert outcome.reason == GateReason.HALLUCINATION


async def test_an_unsupported_language_asks_again() -> None:
    outcome = await run(FakeSTT([fake_result(GOOD, language="fr")]), [])
    assert outcome.reason == GateReason.UNSUPPORTED_LANGUAGE


async def test_the_audio_is_deleted_also_when_the_transcript_is_refused() -> None:
    events: list[str] = []
    await run(FakeSTT([fake_result(GOOD, confidence=0.1)]), events)
    assert events == ["open", "closed"]


# ---- the length of the audio fills in a missing duration


LONG_TEXT = "ye wali watch ka price kya hai batao"  # 8 words


async def test_missing_duration_is_taken_from_the_audio() -> None:
    """The provider gave no duration (0). Eight words in half a second is not human speech."""
    result = fake_result(LONG_TEXT, duration_s=0.0)
    outcome = await run(FakeSTT([result]), [], duration_s=0.5)
    assert outcome.reason == GateReason.HALLUCINATION


async def test_missing_duration_with_a_normal_clip_passes() -> None:
    result = fake_result(LONG_TEXT, duration_s=0.0)
    outcome = await run(FakeSTT([result]), [], duration_s=4.0)
    assert outcome.ok is True
    assert outcome.transcript is not None
    assert outcome.transcript.duration_s == 4.0


# ---- errors that the worker must see


@pytest.mark.parametrize("error", [UnsupportedMedia("image"), WhatsAppTemporaryError("HTTP 500")])
async def test_download_errors_are_raised(error: Exception) -> None:
    with pytest.raises(type(error)):
        await run(FakeSTT([GOOD]), [], error=error)


async def test_a_provider_failure_is_raised_and_the_audio_is_still_deleted() -> None:
    events: list[str] = []
    with pytest.raises(TranscriptionError):
        await run(FakeSTT([TranscriptionError("boom")]), events)
    assert events == ["open", "closed"]
