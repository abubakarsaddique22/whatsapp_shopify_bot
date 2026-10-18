"""Phase 6, step 2: the confidence gate for voice notes (Blueprint 6.8).

A doubtful transcript must never reach the router as if it were certain. These pure functions
decide whether to go on or to ask the customer politely to repeat. Nothing here calls a model,
the network or the database.

    decision = check_audio_limits(duration_s, size_bytes, max_seconds=90, max_bytes=5_000_000)
    decision = check_transcript(result, min_words=2, min_confidence=0.0)
    text = clarification_message(decision.reason, reply_lang)

Ask again when any of these is true:
  - the audio is longer or larger than allowed (check_audio_limits)
  - the transcript is empty or has fewer words than STT_MIN_WORDS
  - the provider confidence is below STT_MIN_CONFIDENCE (0.0 means: do not check)
  - the detected language is not English, Urdu or Punjabi
  - the text looks like a speech-model hallucination (the same phrase repeated, or far more
    words than a clip of that length could hold)
  - later, in the pipeline: the router is unsure about the transcript (router_is_doubtful)

The numbers that are not settings yet (REPEAT_*, MAX_WORDS_PER_SECOND, ALLOWED_LANGUAGES) are
first guesses. The bake-off (blueprint 6.6) decides the final values; then they move to settings.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from app.integrations.speech.base import TranscriptionResult
from app.language.policy import ReplyLang

ALLOWED_LANGUAGES = frozenset({"en", "ur", "pa"})
# Whisper-style models often label Pakistani Urdu as Hindi ("hi"). The blueprint rule is strict
# on purpose; measure how often that happens in the bake-off before you allow "hi".

REPEAT_MIN_WORDS = 6  # a shorter text is never judged a "repeated phrase"
REPEAT_MIN_TIMES = 3  # the same phrase this many times in a row ...
REPEAT_SHARE = 0.6  # ... and covering at least this share of all words
MAX_PHRASE_WORDS = 4  # longest repeated phrase we look for
MAX_WORDS_PER_SECOND = 6.5  # faster than any speaker: the clip cannot hold this many words
SPEED_CHECK_MIN_WORDS = 5

_LANGUAGE_NAMES = {
    "english": "en",
    "urdu": "ur",
    "punjabi": "pa",
    "panjabi": "pa",
    "hindi": "hi",
}
_WORD = re.compile(r"[\w']+")  # \w covers Latin and Arabic-script letters


class GateReason(StrEnum):
    EMPTY = "empty"
    TOO_SHORT = "too_short"
    LOW_CONFIDENCE = "low_confidence"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    HALLUCINATION = "hallucination"
    TOO_LONG = "too_long"
    TOO_LARGE = "too_large"
    ROUTER_DOUBT = "router_doubt"


@dataclass(frozen=True)
class GateDecision:
    ok: bool
    reason: GateReason | None = None


PASS = GateDecision(ok=True)


def _stop(reason: GateReason) -> GateDecision:
    return GateDecision(ok=False, reason=reason)


# ---- audio limits (before transcription)


def check_audio_limits(
    duration_s: float | None,
    size_bytes: int | None,
    *,
    max_seconds: int,
    max_bytes: int,
) -> GateDecision:
    """Too long or too big: do not even send it to the speech provider. None = not known yet."""
    if size_bytes is not None and size_bytes > max_bytes:
        return _stop(GateReason.TOO_LARGE)
    if duration_s is not None and duration_s > max_seconds:
        return _stop(GateReason.TOO_LONG)
    return PASS


# ---- transcript checks (after transcription)


def words_of(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def normalize_language(code: str | None) -> str | None:
    """'en-US' -> 'en', 'ur_PK' -> 'ur', 'Urdu' -> 'ur'. None stays None."""
    if not code:
        return None
    lowered = code.strip().lower()
    if lowered in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[lowered]
    return re.split(r"[-_]", lowered)[0]


def _has_repeated_phrase(words: list[str]) -> bool:
    total = len(words)
    if total < REPEAT_MIN_WORDS:
        return False
    for size in range(1, MAX_PHRASE_WORDS + 1):
        for start in range(total - size + 1):
            block = words[start : start + size]
            repeats = 1
            nxt = start + size
            while nxt + size <= total and words[nxt : nxt + size] == block:
                repeats += 1
                nxt += size
            if repeats >= REPEAT_MIN_TIMES and repeats * size >= REPEAT_SHARE * total:
                return True
    return False


def looks_like_hallucination(text: str, duration_s: float = 0.0) -> bool:
    """Speech models invent text on silence or noise: one phrase again and again, or a long
    text from a clip far too short to hold it."""
    words = words_of(text)
    if _has_repeated_phrase(words):
        return True
    if duration_s > 0 and len(words) >= SPEED_CHECK_MIN_WORDS:
        return len(words) / duration_s > MAX_WORDS_PER_SECOND
    return False


def check_transcript(
    result: TranscriptionResult,
    *,
    min_words: int,
    min_confidence: float,
    allowed_languages: frozenset[str] = ALLOWED_LANGUAGES,
) -> GateDecision:
    """Go on with this transcript, or ask the customer to repeat. First failing rule wins."""
    words = words_of(result.text)
    if not words:
        return _stop(GateReason.EMPTY)
    if len(words) < min_words:
        return _stop(GateReason.TOO_SHORT)
    if result.confidence is not None and result.confidence < min_confidence:
        return _stop(GateReason.LOW_CONFIDENCE)
    language = normalize_language(result.language)
    if language is not None and language not in allowed_languages:
        return _stop(GateReason.UNSUPPORTED_LANGUAGE)
    if looks_like_hallucination(result.text, result.duration_s):
        return _stop(GateReason.HALLUCINATION)
    return PASS


def router_is_doubtful(router_confidence: float, threshold: float) -> bool:
    """After routing: the router itself is unsure about the transcript."""
    return router_confidence < threshold


# ---- what we say to the customer (synthetic text, blueprint 6.8)

_UNCLEAR_EN = (
    "Sorry, I could not hear that clearly. Could you please send it again or type your question?"
)
_UNCLEAR_RU = (
    "Maazrat, awaz saaf sunai nahi di. Meherbani karke dobara bhej dein ya likh kar bata dein."
)
_LONG_EN = "Sorry, that voice message is too long. Please send a shorter one or type your question."
_LONG_RU = (
    "Maazrat, voice message bohat lambi hai. Meherbani karke chhoti voice message bhejein "
    "ya likh kar bata dein."
)


def clarification_message(reason: GateReason | None, reply_lang: ReplyLang = "en_roman_ur") -> str:
    """The polite request to repeat. 'en' and 'roman_ur' get one line; anything else (unknown
    language, a doubtful transcript) gets the short English line plus the Roman Urdu line."""
    too_big = reason in (GateReason.TOO_LONG, GateReason.TOO_LARGE)
    english = _LONG_EN if too_big else _UNCLEAR_EN
    roman_urdu = _LONG_RU if too_big else _UNCLEAR_RU
    if reply_lang == "en":
        return english
    if reply_lang == "roman_ur":
        return roman_urdu
    return f"{english}\n{roman_urdu}"
