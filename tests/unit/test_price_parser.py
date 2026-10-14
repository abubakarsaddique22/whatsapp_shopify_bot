"""Price parser: Roman Urdu and English price words -> whole numbers (Phase 3, step 1)."""

import pytest

from app.language.price_parser import parse_price


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # short forms
        ("10k", 10_000),
        ("under 10K", 10_000),
        ("10 k", 10_000),
        # Roman Urdu number words
        ("das hazar", 10_000),
        ("das hazaar wali watch", 10_000),
        ("10 hazar", 10_000),
        ("pachas hazar", 50_000),
        ("dhai hazar", 2_500),
        ("dedh lakh", 150_000),
        ("ek lakh se kam", 100_000),
        # lakh / thousand
        ("1 lakh", 100_000),
        ("1.5 lakh", 150_000),
        ("2.5 lakh", 250_000),
        ("under 10 thousand", 10_000),
        # plain digits and currency words
        ("15000", 15_000),
        ("15,000", 15_000),
        ("Rs 12,500", 12_500),
        ("Rs12,500", 12_500),
        ("PKR 8,000", 8_000),
        # inside a real customer sentence
        ("black casio watch under 10k", 10_000),
        ("kala watch 10k se kam", 10_000),
        # first price wins
        ("5000 se 10000 tak", 5_000),
        # model codes are not prices
        ("casio ga2100 under 10k", 10_000),
        ("casio GA-2100 under 10k", 10_000),
    ],
)
def test_parse_price_found(text: str, expected: int):
    assert parse_price(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hello",
        "black watch",
        "40mm watch",  # a case size, not a price
        "size 42",
        "10 kala watch",  # the 'k' in 'kala' is not a unit
        "casio ga2100",  # glued model code
        "casio GA-2100",  # model code with a hyphen
        "What is the price of the GA-2100?",
        "dw-5600 kitne ka hai?",
        "mujhe do kala watch chahiye",  # 'do' is a word here, not a number
    ],
)
def test_parse_price_not_a_price(text: str):
    assert parse_price(text) is None
