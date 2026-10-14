"""Language hint: normalize_input, script_hint, detect_language (Phase 5, step 1)."""

import pytest

from app.domain.models import Lang
from app.language.detect import (
    MAX_INPUT_CHARS,
    detect_language,
    normalize_input,
    script_hint,
)

URDU = "آپ کے پاس کالی گھڑی ہے؟"
PUNJABI = "ਤੁਹਾਡੇ ਕੋਲ ਕਾਲੀ ਘੜੀ ਹੈ"
HINDI = "आपके पास काली घड़ी है"


# ---- normalize_input -------------------------------------------------------------------


def test_normalize_trims_and_collapses_spaces():
    assert normalize_input("  price   kya \t hai?  ") == "price kya hai?"


def test_normalize_removes_control_and_invisible_characters():
    # NUL, zero-width space, right-to-left mark, carriage return
    assert normalize_input("pri\x00ce\u200b kya\u200f\r\nhai") == "price kya\nhai"


def test_normalize_keeps_urdu_text():
    assert normalize_input(URDU) == URDU


def test_normalize_full_width_letters():
    assert normalize_input("ＧＡ-2100") == "GA-2100"


def test_normalize_limits_blank_lines_and_length():
    assert normalize_input("a\n\n\n\n\nb") == "a\n\nb"
    assert len(normalize_input("x" * (MAX_INPUT_CHARS + 500))) == MAX_INPUT_CHARS


@pytest.mark.parametrize("empty", ["", "   ", "\x00\u200b"])
def test_normalize_empty(empty):
    assert normalize_input(empty) == ""


# ---- script_hint -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("do you have this watch in black", "latin"),
        ("ap ka pass black color hai?", "latin"),
        (URDU, "arabic_script"),
        (PUNJABI, "gurmukhi"),
        (HINDI, "devanagari"),
        ("", "latin"),
        ("12345 ???", "latin"),
        ("price کتنی ہے؟", "arabic_script"),  # 6 Arabic letters of 11
        ("GA-2100 price kya hai ہے", "latin"),  # a few Arabic letters do not win
    ],
)
def test_script_hint(text, expected):
    assert script_hint(text) == expected


# ---- detect_language: Latin script -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "do you have this watch in black",
        "what is the price of the GA-2100?",
        "Please send me the picture",
        "Price?",
        "available?",
    ],
)
def test_english(text):
    assert detect_language(text).language == Lang.EN


@pytest.mark.parametrize(
    "text",
    [
        "ap ka pass black color hai?",
        "is ki price kya hai?",
        "mujhe is watch ki picture bhejo",
        "delivery kitne din mein hogi?",
        "10k ke andar koi watch hai?",
        "mera order kahan hai?",
        "refund milta hai?",
        "Salam",
    ],
)
def test_roman_urdu(text):
    assert detect_language(text).language == Lang.ROMAN_UR


def test_mixed_english_and_roman_urdu():
    hint = detect_language("black wali available hai?")
    assert hint.language == Lang.MIXED
    assert hint.roman_ur_hits == 2
    assert hint.english_hits == 1


@pytest.mark.parametrize("text", ["pic", "hello", "GA-2100", "40mm", "", "   ", "😀"])
def test_no_evidence_is_unknown_not_a_guess(text):
    hint = detect_language(text)
    assert hint.language == Lang.UNKNOWN
    assert hint.roman_ur_share is None


def test_spelling_variants_and_repeated_letters():
    assert detect_language("kitnaaa hai").roman_ur_hits == 2  # kitna, hai
    assert detect_language("haiiii?").roman_ur_hits == 1
    assert detect_language("KITNY paisay? kya rate hy").language == Lang.ROMAN_UR


def test_word_in_both_lists_is_ignored():
    # 'is' and 'me' are English AND Roman Urdu ('mein'). They must not move the score.
    hint = detect_language("send me is")
    assert hint.roman_ur_hits == 0
    assert hint.english_hits == 1  # only 'send'


def test_hint_reports_the_share():
    hint = detect_language("ap ka pass black color hai?")
    assert (hint.roman_ur_hits, hint.english_hits, hint.roman_ur_share) == (3, 1, 0.75)


# ---- detect_language: other scripts ----------------------------------------------------


def test_urdu_script():
    hint = detect_language(URDU)
    assert (hint.script, hint.language) == ("arabic_script", Lang.UR)


def test_urdu_script_with_a_few_english_words_stays_urdu():
    assert detect_language("آپ کے پاس GA-2100 ہے کیا؟").language == Lang.UR


def test_urdu_and_english_together_is_mixed():
    assert detect_language("price کتنی ہے؟").language == Lang.MIXED


def test_punjabi_script():
    assert detect_language(PUNJABI).language == Lang.PA


def test_hindi_script_is_not_supported():
    assert detect_language(HINDI).language == Lang.UNKNOWN


def test_detection_normalises_first():
    # invisible characters inside the words must not break the lexicon match
    assert detect_language("kya\u200b price hai").roman_ur_hits == 2
