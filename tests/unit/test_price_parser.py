"""Price parser: Roman Urdu and English price words -> whole numbers (Phase 3, step 1)."""

import pytest

from app.language.price_parser import parse_budget, parse_price


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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Do you have any watch under 15000?", 15_000),
        ("15000 se kam", 15_000),
        ("15,000 tak koi watch", 15_000),
        ("10k ke andar koi watch hai?", 10_000),
        ("das hazar tak koi acchi watch dikhao", 10_000),
        ("I need a watch under 20k, kala color mein chahiye", 20_000),
        ("Rs 12000 wali watch", 12_000),  # a currency makes it a price
        ("Rolex 116610 under 20k", 20_000),  # the unit price wins over the model number
        ("budget 25000 hai", 25_000),
    ],
)
def test_parse_budget_found(text: str, expected: int):
    assert parse_budget(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Rolex Submariner 16610 ki price?",  # live bug 2026-10-20: model number read as a limit
        "16610 available hai?",
        "116610LN price",
        "Seiko 5 SNK809",
        "black watch",
    ],
)
def test_parse_budget_model_number_is_no_budget(text: str):
    assert parse_budget(text) is None
