"""The voice confidence gate (Phase 6, step 2). T22: an unclear transcript gets a polite request
to repeat and nothing is answered from it. Pure functions: no network, no files."""

import pytest

from app.integrations.speech.fake_stt import fake_result
from app.voice.gate import (
    GateReason,
    check_audio_limits,
    check_transcript,
    clarification_message,
    looks_like_hallucination,
    normalize_language,
    router_is_doubtful,
)

MIN_WORDS = 2
MIN_CONF = 0.5


def check(text: str, **fields: object):
    return check_transcript(
        fake_result(text, **fields), min_words=MIN_WORDS, min_confidence=MIN_CONF
    )


# ---- a good transcript passes


@pytest.mark.parametrize(
    "text",
    [
        "iski price kya hai",
        "How much is this watch?",
        "black wali available hai kya",
        "ye watch 5000 se kam mein hai",
    ],
)
def test_a_normal_transcript_passes(text: str) -> None:
    decision = check(text)
    assert decision.ok is True
    assert decision.reason is None


# ---- the reasons to ask again


@pytest.mark.parametrize("text", ["", "   ", "...", "!?"])
def test_empty_transcript(text: str) -> None:
    assert check(text).reason == GateReason.EMPTY


def test_too_short_transcript() -> None:
    assert check("price").reason == GateReason.TOO_SHORT


def test_low_confidence() -> None:
    decision = check("iski price kya hai", confidence=0.2)
    assert (decision.ok, decision.reason) == (False, GateReason.LOW_CONFIDENCE)


def test_missing_confidence_is_not_a_reason_to_stop() -> None:
    assert check("iski price kya hai", confidence=None).ok is True


def test_confidence_check_is_off_when_the_threshold_is_zero() -> None:
    result = fake_result("iski price kya hai", confidence=0.01)
    assert check_transcript(result, min_words=MIN_WORDS, min_confidence=0.0).ok is True


@pytest.mark.parametrize("language", ["hi", "fr", "ar", "Hindi"])
def test_unsupported_language(language: str) -> None:
    decision = check("iski price kya hai", language=language)
    assert decision.reason == GateReason.UNSUPPORTED_LANGUAGE


@pytest.mark.parametrize("language", ["en", "en-US", "ur", "ur_PK", "pa", "Punjabi", "Urdu", None])
def test_supported_or_unknown_language_passes(language: str | None) -> None:
    assert check("iski price kya hai", language=language).ok is True


def test_repeated_phrase_is_a_hallucination() -> None:
    decision = check("thank you thank you thank you thank you")
    assert decision.reason == GateReason.HALLUCINATION


def test_long_text_from_a_tiny_clip_is_a_hallucination() -> None:
    decision = check("ye wali watch ka price kya hai batao", duration_s=0.5)
    assert decision.reason == GateReason.HALLUCINATION


def test_the_same_text_from_a_normal_clip_passes() -> None:
    assert check("ye wali watch ka price kya hai batao", duration_s=4.0).ok is True


def test_first_failing_rule_wins() -> None:
    """Empty text is reported as empty, not as the low confidence it also has."""
    assert check("", confidence=0.1).reason == GateReason.EMPTY


# ---- hallucination detector on its own


@pytest.mark.parametrize(
    "text",
    [
        "price price price kya hai batao",  # a word three times, but not most of the text
        "haan haan",  # too short to judge
        "iski price kya hai aur colour kon sa hai",
    ],
)
def test_normal_speech_is_not_flagged(text: str) -> None:
    assert looks_like_hallucination(text, duration_s=5.0) is False


def test_unknown_duration_skips_the_speed_check() -> None:
    assert looks_like_hallucination("ye wali watch ka price kya hai batao", 0.0) is False


# ---- audio limits (before transcription)


def test_audio_within_limits_passes() -> None:
    assert check_audio_limits(30.0, 100_000, max_seconds=90, max_bytes=5_000_000).ok is True


def test_audio_too_long() -> None:
    decision = check_audio_limits(91.0, 100_000, max_seconds=90, max_bytes=5_000_000)
    assert decision.reason == GateReason.TOO_LONG


def test_audio_too_large() -> None:
    decision = check_audio_limits(10.0, 5_000_001, max_seconds=90, max_bytes=5_000_000)
    assert decision.reason == GateReason.TOO_LARGE


def test_audio_exactly_at_the_limit_passes() -> None:
    assert check_audio_limits(90.0, 5_000_000, max_seconds=90, max_bytes=5_000_000).ok is True


def test_unknown_duration_and_size_pass() -> None:
    assert check_audio_limits(None, None, max_seconds=90, max_bytes=5_000_000).ok is True


# ---- language names


@pytest.mark.parametrize(
    ("code", "expected"),
    [("en-US", "en"), ("ur_PK", "ur"), ("Urdu", "ur"), ("PA", "pa"), ("", None), (None, None)],
)
def test_normalize_language(code: str | None, expected: str | None) -> None:
    assert normalize_language(code) == expected


# ---- router doubt


def test_router_doubt() -> None:
    assert router_is_doubtful(0.4, 0.6) is True
    assert router_is_doubtful(0.6, 0.6) is False


# ---- what the customer is told


def test_clarification_in_english() -> None:
    text = clarification_message(GateReason.EMPTY, "en")
    assert text.startswith("Sorry, I could not hear that clearly")
    assert "\n" not in text


def test_clarification_in_roman_urdu() -> None:
    text = clarification_message(GateReason.LOW_CONFIDENCE, "roman_ur")
    assert text.startswith("Maazrat, awaz saaf sunai nahi di")
    assert "\n" not in text


@pytest.mark.parametrize("reply_lang", ["en_roman_ur", "mixed", "ur"])
def test_clarification_otherwise_has_both_lines(reply_lang: str) -> None:
    text = clarification_message(GateReason.HALLUCINATION, reply_lang)  # type: ignore[arg-type]
    english, roman_urdu = text.split("\n")
    assert english.startswith("Sorry")
    assert roman_urdu.startswith("Maazrat")


def test_default_clarification_is_bilingual() -> None:
    assert "\n" in clarification_message(GateReason.EMPTY)


@pytest.mark.parametrize("reason", [GateReason.TOO_LONG, GateReason.TOO_LARGE])
def test_too_long_has_its_own_message(reason: GateReason) -> None:
    assert "too long" in clarification_message(reason, "en")
    assert "lambi" in clarification_message(reason, "roman_ur")
