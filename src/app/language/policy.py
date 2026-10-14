"""Phase 5, step 2: the language policy (Blueprint 6.1) as plain functions.

asked_language   did the customer ask for a language ('English mein bataein')?
next_language    sticky language: which language the conversation is in after this message.
reply_language   language policy table 6.1: sticky language -> language of the reply.
reply_matches    cheap output check (6.2, layer 4): is the reply really in the target language?

No state is stored here and nothing calls a model or the network. The caller passes in what it
already has: `state.language` (current sticky language) and `state.last_router_output.language`
(language of the PREVIOUS customer message), then saves the result back into the state.
"""

import re
from typing import Literal

from app.domain.models import Lang
from app.language.detect import detect_language

# What the bot answers in. "en_roman_ur" = one short line in English plus one in Roman Urdu
# (used when we do not know the language, or a voice note was unclear).
ReplyLang = Literal["en", "roman_ur", "mixed", "ur", "en_roman_ur"]

SWITCH_AFTER = 2  # blueprint 6.1: two messages in a row in another language switch the language
MIN_HITS_FOR_MISMATCH = 3  # a reply needs this many wrong-language words before it is flagged

_ASK_ENGLISH = re.compile(
    r"\b(?:in english|english please|speak english|"
    r"english (?:mein|me|main|may) (?:bata\w*|baat\w*|likh\w*|jawab|reply|bol\w*))\b"
)
_ASK_ROMAN_UR = re.compile(
    r"\b(?:roman urdu|urdu please|"
    r"urdu (?:mein|me|main|may) (?:bata\w*|baat\w*|likh\w*|jawab|reply|bol\w*))\b"
)


def asked_language(text: str) -> Lang | None:
    """Explicit request in the message ('English mein bataein') -> that language, else None."""
    lowered = (text or "").lower()
    if _ASK_ENGLISH.search(lowered):
        return Lang.EN
    if _ASK_ROMAN_UR.search(lowered):
        return Lang.ROMAN_UR
    return None


def next_language(current: Lang, previous: Lang | None, detected: Lang, text: str = "") -> Lang:
    """Sticky language after this message.

    current   conversation language so far (state.language, UNKNOWN for a new conversation)
    previous  detected language of the previous customer message (None if there was none)
    detected  detected language of this message
    text      this message, only used to look for an explicit request

    - An explicit request always wins.
    - UNKNOWN ('pic', 'hello', Hindi) never changes anything: the old language stays.
    - A new conversation takes the first usable language at once.
    - A different language needs SWITCH_AFTER messages in a row; one stray message is ignored.
    """
    asked = asked_language(text)
    if asked is not None:
        return asked
    if detected == Lang.UNKNOWN:
        return current
    if current == Lang.UNKNOWN or detected == current:
        return detected
    if detected == previous:  # second message in a row in the new language
        return detected
    return current


def reply_language(
    language: Lang, *, urdu_script_replies: bool = False, low_confidence: bool = False
) -> ReplyLang:
    """Table 6.1: conversation language -> reply language.

    Urdu script and Punjabi get Roman Urdu (A4: needs client confirmation); a setting can turn
    on Urdu-script replies for Urdu. Unknown language or a doubtful voice transcript gets the
    short bilingual line (ask to repeat / offer a human). Never a voice reply.
    """
    if low_confidence or language == Lang.UNKNOWN:
        return "en_roman_ur"
    if language == Lang.EN:
        return "en"
    if language == Lang.MIXED:
        return "mixed"
    if language == Lang.UR and urdu_script_replies:
        return "ur"
    return "roman_ur"  # roman_ur, ur, pa


def reply_matches(reply: str, target: ReplyLang) -> bool:
    """Cheap check that the reply is in the target language (6.2, layer 4).

    Only clear mistakes fail: a long English answer when Roman Urdu was required, Roman Urdu
    when English was required, or the wrong script. Mixed and bilingual replies always pass
    (as long as they are in Latin script). Brand names and units (Rs, mm) are in neither word
    list, so they do not count.
    """
    hint = detect_language(reply)
    if target == "ur":
        return hint.script == "arabic_script"
    if hint.script != "latin":
        return False
    if target == "en":
        return not (hint.language == Lang.ROMAN_UR and hint.roman_ur_hits >= MIN_HITS_FOR_MISMATCH)
    if target == "roman_ur":
        return not (hint.language == Lang.EN and hint.english_hits >= MIN_HITS_FOR_MISMATCH)
    return True  # mixed, en_roman_ur
