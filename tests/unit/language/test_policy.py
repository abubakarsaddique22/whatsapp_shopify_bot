"""Language policy: sticky language, reply-language table 6.1, output check (Phase 5, step 2)."""

import pytest

from app.domain.models import Lang
from app.language.detect import detect_language
from app.language.policy import (
    asked_language,
    next_language,
    reply_language,
    reply_matches,
)

URDU = "آپ کے پاس کالی گھڑی ہے؟"
HINDI = "आपके पास काली घड़ी है"


# ---- asked_language --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "English mein bataein",
        "english me batao",
        "please reply in English",
        "English please",
        "speak english",
    ],
)
def test_asked_for_english(text):
    assert asked_language(text) == Lang.EN


@pytest.mark.parametrize("text", ["Roman Urdu mein likho", "urdu mein baat karo", "Urdu please"])
def test_asked_for_roman_urdu(text):
    assert asked_language(text) == Lang.ROMAN_UR


@pytest.mark.parametrize("text", ["", "black wali watch hai?", "do you have the GA-2100?"])
def test_no_request(text):
    assert asked_language(text) is None


# ---- next_language: sticky language ----------------------------------------------------


def test_new_conversation_takes_the_first_language():
    assert next_language(Lang.UNKNOWN, None, Lang.ROMAN_UR) == Lang.ROMAN_UR


def test_same_language_stays():
    assert next_language(Lang.EN, Lang.EN, Lang.EN) == Lang.EN


@pytest.mark.parametrize("current", [Lang.EN, Lang.ROMAN_UR, Lang.UNKNOWN])
def test_unknown_message_changes_nothing(current):
    assert next_language(current, Lang.EN, Lang.UNKNOWN) == current


def test_one_message_in_another_language_is_ignored():
    assert next_language(Lang.EN, Lang.EN, Lang.ROMAN_UR) == Lang.EN


def test_two_messages_in_a_row_switch_the_language():
    assert next_language(Lang.EN, Lang.ROMAN_UR, Lang.ROMAN_UR) == Lang.ROMAN_UR
    assert next_language(Lang.EN, Lang.UR, Lang.UR) == Lang.UR
    assert next_language(Lang.EN, Lang.MIXED, Lang.MIXED) == Lang.MIXED


def test_two_different_new_languages_do_not_switch():
    assert next_language(Lang.EN, Lang.ROMAN_UR, Lang.UR) == Lang.EN


def test_unknown_message_in_between_breaks_the_run():
    assert next_language(Lang.EN, Lang.UNKNOWN, Lang.ROMAN_UR) == Lang.EN


def test_no_previous_message_does_not_switch():
    assert next_language(Lang.EN, None, Lang.ROMAN_UR) == Lang.EN


@pytest.mark.parametrize("detected", [Lang.ROMAN_UR, Lang.EN, Lang.UNKNOWN])
def test_explicit_request_wins_at_once(detected):
    assert next_language(Lang.ROMAN_UR, Lang.ROMAN_UR, detected, "English mein bataein") == Lang.EN
    assert next_language(Lang.EN, Lang.EN, detected, "Roman Urdu mein likho") == Lang.ROMAN_UR


# ---- reply_language: table 6.1 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (Lang.EN, "en"),
        (Lang.ROMAN_UR, "roman_ur"),
        (Lang.MIXED, "mixed"),
        (Lang.UR, "roman_ur"),  # Urdu script text -> Roman Urdu (A4)
        (Lang.PA, "roman_ur"),  # Punjabi -> simple Roman Urdu (A4)
        (Lang.UNKNOWN, "en_roman_ur"),  # do not guess: short English plus Roman Urdu
    ],
)
def test_reply_language_table(language, expected):
    assert reply_language(language) == expected


def test_urdu_script_replies_setting_only_changes_urdu():
    assert reply_language(Lang.UR, urdu_script_replies=True) == "ur"
    assert reply_language(Lang.PA, urdu_script_replies=True) == "roman_ur"
    assert reply_language(Lang.EN, urdu_script_replies=True) == "en"
    assert reply_language(Lang.ROMAN_UR, urdu_script_replies=True) == "roman_ur"


@pytest.mark.parametrize("language", list(Lang))
def test_low_confidence_always_asks_in_both_languages(language):
    assert reply_language(language, low_confidence=True) == "en_roman_ur"


# ---- reply_matches: output check -------------------------------------------------------

GOOD_EN = "The GA-2100 is available in black. The price is Rs 12,500."
BAD_EN_FOR_ROMAN = "Do you have the black color? Please send the picture, the price is available."
GOOD_ROMAN = (
    "Ji, ye watch Black color mein available hai. Price Rs 12,500 hai. Kya main picture bhej doon?"
)
BAD_ROMAN_FOR_EN = (
    "Ji, ye watch Black color mein available hai. Kya main picture bhej doon? "
    "Aap ki madad ke liye shukriya"
)
BILINGUAL = "Sorry, I did not understand. Maazrat, aap dobara bata dein?"


def test_english_reply_for_english():
    assert reply_matches(GOOD_EN, "en")


def test_roman_urdu_reply_for_roman_urdu():
    assert reply_matches(GOOD_ROMAN, "roman_ur")


def test_english_paragraph_when_roman_urdu_was_required_fails():
    assert not reply_matches(BAD_EN_FOR_ROMAN, "roman_ur")


def test_roman_urdu_when_english_was_required_fails():
    assert not reply_matches(BAD_ROMAN_FOR_EN, "en")


@pytest.mark.parametrize("target", ["mixed", "en_roman_ur"])
def test_mixed_and_bilingual_replies_always_pass(target):
    assert reply_matches(GOOD_EN, target)
    assert reply_matches(GOOD_ROMAN, target)
    assert reply_matches(BILINGUAL, target)


def test_a_few_stray_words_do_not_fail():
    # brand names, units and one English word inside a Roman Urdu reply are normal
    assert reply_matches("Ji, ye GA-2100 hai, 42 mm. Price Rs 12,500 hai.", "roman_ur")


def test_script_must_match():
    assert reply_matches(URDU, "ur")
    assert not reply_matches(GOOD_EN, "ur")
    assert not reply_matches(URDU, "roman_ur")
    assert not reply_matches(URDU, "en")
    assert not reply_matches(URDU, "mixed")


# ---- the three pieces together ---------------------------------------------------------


def run(messages):
    """What the agent will do for every customer message: detect, sticky, reply language."""
    current, previous, replies = Lang.UNKNOWN, None, []
    for text in messages:
        detected = detect_language(text).language
        current = next_language(current, previous, detected, text)
        previous = detected
        replies.append(reply_language(current))
    return replies


def test_conversation_switches_after_two_roman_urdu_messages():
    replies = run(
        [
            "do you have this watch in black",
            "ap ka pass black color hai?",  # one stray message: still English
            "mujhe is watch ki picture bhejo",  # second in a row: switch
            "pic",  # no evidence: stays Roman Urdu
        ]
    )
    assert replies == ["en", "en", "roman_ur", "roman_ur"]


def test_customer_asks_for_english():
    assert run(["ap ka pass black color hai?", "English mein bataein"]) == ["roman_ur", "en"]


@pytest.mark.parametrize("first_message", ["hello", HINDI])
def test_unknown_first_message_gets_the_bilingual_line(first_message):
    assert run([first_message]) == ["en_roman_ur"]
