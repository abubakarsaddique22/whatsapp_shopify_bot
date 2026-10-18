"""Card-number redaction (Phase 11, T19 support): cards go, phone and order numbers stay."""

import pytest

from app.security.redaction import CARD_PLACEHOLDER, redact_cards


def urdu_digits(text: str) -> str:
    """Write the digits of the text with Urdu-script digits (U+06F0 to U+06F9)."""
    return "".join(chr(0x06F0 + int(c)) if c.isdigit() else c for c in text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("my card is 4111 1111 1111 1111", "my card is [CARD]"),
        ("card 4111-1111-1111-1111.", "card [CARD]."),
        ("4111111111111111", "[CARD]"),
        ("pay with 5555555555554444, thanks", "pay with [CARD], thanks"),
        ("amex 3782 822463 10005 ok", "amex [CARD] ok"),
        ("short visa 4222222222222", "short visa [CARD]"),
        ("1234 5678 9012 3456", "[CARD]"),  # four groups of four: always treated as a card
        ("4111 1111 1111 1111 5000", "[CARD] 5000"),  # an amount after the card is kept
        ("0300 1234567 4111 1111 1111 1111", "0300 1234567 [CARD]"),  # phone first, then card
    ],
)
def test_card_numbers_are_replaced(text, expected):
    assert redact_cards(text) == expected


def test_urdu_script_digits_are_found_too():
    assert redact_cards(urdu_digits("4111111111111111")) == CARD_PLACEHOLDER


@pytest.mark.parametrize(
    "text",
    [
        "no card here",
        "phone 03001234567",
        "phone +92 300 1234567",
        "phone 00923001234567",  # 14 digits, but the checksum fails
        "order 1234567890123 total 12,500",
        "price 1234567890123.45",
        "decimal 0.4111111111111111",
        "ref a4111111111111111",
        "wrong checksum 4111111111111112",
        "Rs 12,500 ka watch",
        "",
    ],
)
def test_other_numbers_are_left_alone(text):
    assert redact_cards(text) == text


def test_the_placeholder_is_the_signal_for_the_worker():
    assert CARD_PLACEHOLDER in redact_cards("meri card 4111 1111 1111 1111 hai")
    assert CARD_PLACEHOLDER not in redact_cards("meri watch kitni hai")


def test_two_cards_in_one_message():
    text = "4111111111111111 aur 5555 5555 5555 4444"
    assert redact_cards(text) == "[CARD] aur [CARD]"
