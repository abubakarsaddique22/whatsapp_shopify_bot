"""Deterministic price parser: '10k', 'das hazar', '1 lakh', 'Rs 12,500' -> whole number.

Why not leave this to the LLM? Roman Urdu number words are easy to get wrong ('das hazar' is
10,000 but 'das' alone is 10). A small rule-based parser is exact and testable (Blueprint 8.2,
router prompt rule 7). The router's `max_price` can be checked against it.

Rules:
- The first price found in the text wins ('5000 se 10000' -> 5000).
- A bare number below MIN_BARE_PRICE is NOT a price ('40mm', 'size 42'). With a unit
  ('10k', '1 lakh') any size is accepted.
- A number glued to letters is not a price ('ga2100' is a model code).
- Returns None when there is no price. Money is a whole number in the store currency.
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache

from app.language.lexicon import get_lexicon

MIN_BARE_PRICE = 500  # a bare number below this is a size, a year fragment or a count
MAX_PRICE = 100_000_000  # sanity limit: anything above is treated as "no price"

# Units that are always understood. config/roman_urdu_lexicon.yaml `numbers` is added on top.
_BUILTIN_UNITS: dict[str, int] = {
    "k": 1_000,
    "thousand": 1_000,
    "hazar": 1_000,
    "hazaar": 1_000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "lac": 100_000,
    "lacs": 100_000,
    "sau": 100,
}

# Number words that can stand before a unit: "das hazar" = 10 x 1000.
NUMBER_WORDS: dict[str, Decimal] = {
    **{
        word: Decimal(value)
        for word, value in {
            "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "panch": 5, "paanch": 5,
            "che": 6, "chay": 6, "chhe": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10,
            "gyarah": 11, "barah": 12, "terah": 13, "chaudah": 14, "pandrah": 15,
            "solah": 16, "satrah": 17, "atharah": 18, "unees": 19, "bees": 20,
            "pachees": 25, "tees": 30, "chalees": 40, "pachas": 50, "saath": 60,
            "sattar": 70, "assi": 80, "nabbay": 90,
        }.items()
    },
    "dedh": Decimal("1.5"),
    "dhai": Decimal("2.5"),
}

# "Rs 12,500", "Rs.12500", "PKR 8000": the currency word is dropped before parsing.
_CURRENCY = re.compile(r"\b(?:rs|pkr)\.?\s*(?=\d)")


@lru_cache
def _compiled() -> tuple[re.Pattern[str], re.Pattern[str], dict[str, int]]:
    units = {**_BUILTIN_UNITS, **{k.lower(): v for k, v in get_lexicon().numbers.items()}}
    unit_alt = "|".join(sorted((re.escape(u) for u in units), key=len, reverse=True))
    word_alt = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
    # (?<![a-z\d.,]) = the number must not be glued to a letter or to another number.
    # (?![a-z])      = the unit must be a whole word ('k' in 'kala' is not a unit).
    numeric = re.compile(
        rf"(?<![a-z\d.,])(?P<digits>\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.(?P<frac>\d+))?"
        rf"(?:\s*(?P<unit>{unit_alt})(?![a-z]))?"
    )
    words = re.compile(rf"\b(?P<word>{word_alt})\s+(?P<unit>{unit_alt})(?![a-z])")
    return numeric, words, units


def _to_price(value: Decimal) -> int | None:
    try:
        number = int(value.to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None
    return number if 0 < number <= MAX_PRICE else None


def parse_price(text: str) -> int | None:
    """First price in `text` as a whole number, or None. See the module docstring for rules."""
    if not text:
        return None
    cleaned = _CURRENCY.sub(" ", text.lower())
    numeric, words, units = _compiled()
    found: list[tuple[int, int]] = []  # (position in text, price)

    for m in numeric.finditer(cleaned):
        number = Decimal(m["digits"].replace(",", "") + (f".{m['frac']}" if m["frac"] else ""))
        unit = m["unit"]
        if unit is None and number < MIN_BARE_PRICE:
            continue
        price = _to_price(number * (units[unit] if unit else 1))
        if price:
            found.append((m.start(), price))

    for m in words.finditer(cleaned):
        price = _to_price(NUMBER_WORDS[m["word"]] * units[m["unit"]])
        if price:
            found.append((m.start(), price))

    return min(found)[1] if found else None
