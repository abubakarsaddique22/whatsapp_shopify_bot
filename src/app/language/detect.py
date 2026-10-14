"""Phase 5, step 1: a cheap language hint (Blueprint 6.2, layers 1 and 2).

normalize_input  clean the text before anything else sees it (first node in Figure 3).
script_hint      which writing system dominates (Unicode ranges).
detect_language  script hint plus Roman Urdu lexicon score -> LanguageHint.

This is only a HINT. The router LLM makes the final decision (it also sees this hint), and the
hint is a cheap test oracle for the golden set. Nothing here calls a model or the network.

Scoring rules (Latin script):
- A token counts for Roman Urdu if it is in `roman_urdu_words`, for English if it is in
  `english_stopwords`. A word in BOTH lists ('is', 'me') says nothing and is ignored.
- Repeated letters are collapsed before scoring ('kitnaaa' -> 'kitna', 'haiii' -> 'hai').
- The original text is never changed for the LLM: only the scoring sees the cleaned tokens.
- The share thresholds below are STARTING values. Tune them with the golden set (Step 5).
"""

import re
import unicodedata
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel

from app.domain.models import Lang
from app.language.lexicon import get_lexicon

ScriptHint = Literal["latin", "arabic_script", "gurmukhi", "devanagari"]

MAX_INPUT_CHARS = 1000  # one WhatsApp question is short; longer text is cut, not rejected
SCRIPT_SHARE_MIN = 0.3  # a script wins when more than this share of the letters belongs to it
ROMAN_UR_SHARE_MIN = 0.7  # Roman Urdu tokens / all scored tokens: at or above = roman_ur
ROMAN_UR_SHARE_MAX = 0.3  # at or below = en; in between = mixed

_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")
_REPEATED_LETTERS = re.compile(r"(.)\1{2,}")  # three or more of the same character


class LanguageHint(BaseModel):
    script: ScriptHint
    language: Lang
    roman_ur_hits: int = 0
    english_hits: int = 0
    roman_ur_share: float | None = None  # roman_ur_hits / (roman_ur_hits + english_hits)


def normalize_input(text: str, max_chars: int = MAX_INPUT_CHARS) -> str:
    """Trim, drop control and invisible characters, collapse spaces, cap the length.

    Unicode category C removes control characters and zero-width / direction marks, which are
    also a way to hide instructions in a message. NFKC turns full-width letters and Arabic
    presentation forms into the normal ones.
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(
        ch for ch in text if ch in "\n\t" or not unicodedata.category(ch).startswith("C")
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_chars].rstrip()


def _letters_in(text: str, low: str, high: str) -> int:
    return sum(ch.isalpha() and low <= ch <= high for ch in text)


def script_hint(text: str) -> ScriptHint:
    """Dominant script by Unicode range (Blueprint 6.2). Empty or digits only -> 'latin'."""
    letters = sum(ch.isalpha() for ch in text) or 1
    if _letters_in(text, "\u0600", "\u06ff") / letters > SCRIPT_SHARE_MIN:
        return "arabic_script"
    if _letters_in(text, "\u0a00", "\u0a7f") / letters > SCRIPT_SHARE_MIN:
        return "gurmukhi"
    if _letters_in(text, "\u0900", "\u097f") / letters > SCRIPT_SHARE_MIN:
        return "devanagari"
    return "latin"


@lru_cache
def _scoring_sets() -> tuple[frozenset[str], frozenset[str]]:
    lexicon = get_lexicon()
    ambiguous = lexicon.roman_urdu_words & lexicon.english_stopwords
    return (
        frozenset(lexicon.roman_urdu_words - ambiguous),
        frozenset(lexicon.english_stopwords - ambiguous),
    )


def _latin_share(text: str) -> float:
    letters = sum(ch.isalpha() for ch in text) or 1
    return sum(ch.isalpha() and ch.isascii() for ch in text) / letters


def _score_latin(text: str) -> LanguageHint:
    roman_words, english_words = _scoring_sets()
    tokens = _WORD.findall(_REPEATED_LETTERS.sub(r"\1", text.lower()))
    roman_hits = sum(token in roman_words for token in tokens)
    english_hits = sum(token in english_words for token in tokens)
    scored = roman_hits + english_hits
    if scored == 0:  # 'pic', 'hello', 'GA-2100': no evidence, so do not guess
        return LanguageHint(script="latin", language=Lang.UNKNOWN)
    share = roman_hits / scored
    if share >= ROMAN_UR_SHARE_MIN:
        language = Lang.ROMAN_UR
    elif share <= ROMAN_UR_SHARE_MAX:
        language = Lang.EN
    else:
        language = Lang.MIXED
    return LanguageHint(
        script="latin",
        language=language,
        roman_ur_hits=roman_hits,
        english_hits=english_hits,
        roman_ur_share=round(share, 2),
    )


def detect_language(text: str) -> LanguageHint:
    """Script check, then (for Latin script) Roman Urdu versus English token scoring.

    Arabic script -> ur, or mixed when a lot of Latin letters are there too ('price کتنی ہے؟').
    Gurmukhi -> pa. Devanagari (Hindi) is not supported -> unknown. Latin with no evidence
    -> unknown; the policy (step 2) then keeps the sticky language of the conversation.
    """
    text = normalize_input(text)
    script = script_hint(text)
    if script == "arabic_script":
        mixed = _latin_share(text) >= SCRIPT_SHARE_MIN
        return LanguageHint(script=script, language=Lang.MIXED if mixed else Lang.UR)
    if script == "gurmukhi":
        return LanguageHint(script=script, language=Lang.PA)
    if script == "devanagari":
        return LanguageHint(script=script, language=Lang.UNKNOWN)
    return _score_latin(text)
