"""Deterministic price parser: '10k', 'das hazar', '1 lakh', 'Rs 12,500' -> whole number.

Why not leave this to the LLM? Roman Urdu number words are easy to get wrong ('das hazar' is
10,000 but 'das' alone is 10). A small rule-based parser is exact and testable (Blueprint 8.2,
router prompt rule 7). The router's `max_price` can be checked against it.

Rules:
- The first price found in the text wins ('5000 se 10000' -> 5000).
- A bare number below MIN_BARE_PRICE is NOT a price ('40mm', 'size 42'). With a unit
  ('10k', '1 lakh') any size is accepted.
- A number glued to letters or a hyphen is not a price ('ga2100', 'GA-2100' are model codes).
- Returns None when there is no price. Money is a whole number in the store currency.

parse_budget is the stricter version for a price LIMIT: a bare number ('16610') is often a watch
model number ('Rolex 16610 ki price?'), so it counts only next to a budget word ('under 15000',
'15000 tak'). A unit ('10k', 'das hazar') or a currency ('Rs 15000') always counts.
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
            "ek": 1,
            "do": 2,
            "teen": 3,
            "char": 4,
            "chaar": 4,
            "panch": 5,
            "paanch": 5,
            "che": 6,
            "chay": 6,
            "chhe": 6,
            "saat": 7,
            "aath": 8,
            "nau": 9,
            "das": 10,
            "gyarah": 11,
            "barah": 12,
            "terah": 13,
            "chaudah": 14,
            "pandrah": 15,
            "solah": 16,
            "satrah": 17,
            "atharah": 18,
            "unees": 19,
            "bees": 20,
            "pachees": 25,
            "tees": 30,
            "chalees": 40,
            "pachas": 50,
            "saath": 60,
            "sattar": 70,
            "assi": 80,
            "nabbay": 90,
        }.items()
    },
    "dedh": Decimal("1.5"),
    "dhai": Decimal("2.5"),
}

# "Rs 12,500", "Rs.12500", "PKR 8000": the currency word is dropped before parsing.
_CURRENCY = re.compile(r"\b(?:rs|pkr)\.?\s*(?=\d)")

# words that make a bare number a budget: 'under 15000', '15000 se kam', '15000 tak'
BUDGET_WORDS = frozenset(
    {"under", "below", "within", "upto", "max", "maximum", "budget", "less", "kam", "tak", "andar"}
)
_WORD = re.compile(r"[a-z]+")


@lru_cache
def _compiled() -> tuple[re.Pattern[str], re.Pattern[str], dict[str, int]]:
    units = {**_BUILTIN_UNITS, **{k.lower(): v for k, v in get_lexicon().numbers.items()}}
    unit_alt = "|".join(sorted((re.escape(u) for u in units), key=len, reverse=True))
    word_alt = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
    # (?<![a-z\d.,-]) = the number must not be glued to a letter, another number or a hyphen
    #                    ('GA-2100' is a model code, not 2100 rupees).
    # (?![a-z])      = the unit must be a whole word ('k' in 'kala' is not a unit).
    numeric = re.compile(
        rf"(?<![a-z\d.,-])(?P<digits>\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.(?P<frac>\d+))?"
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


def _find_prices(text: str) -> list[tuple[int, int, bool]]:
    """Every price in `text` as (position, price, bare). Bare = no unit and no currency."""
    if not text:
        return []
    lowered = text.lower()
    after_currency = {m.end() for m in _CURRENCY.finditer(lowered)}
    # same length as the original, so positions still line up with after_currency
    cleaned = _CURRENCY.sub(lambda m: " " * len(m.group()), lowered)
    numeric, words, units = _compiled()
    found: list[tuple[int, int, bool]] = []

    for m in numeric.finditer(cleaned):
        number = Decimal(m["digits"].replace(",", "") + (f".{m['frac']}" if m["frac"] else ""))
        unit = m["unit"]
        if unit is None and number < MIN_BARE_PRICE:
            continue
        price = _to_price(number * (units[unit] if unit else 1))
        if price:
            found.append((m.start(), price, unit is None and m.start() not in after_currency))

    for m in words.finditer(cleaned):
        price = _to_price(NUMBER_WORDS[m["word"]] * units[m["unit"]])
        if price:
            found.append((m.start(), price, False))

    return sorted(found)


def parse_price(text: str) -> int | None:
    """First price in `text` as a whole number, or None. See the module docstring for rules."""
    found = _find_prices(text)
    return found[0][1] if found else None


def parse_budget(text: str) -> int | None:
    """The customer's price limit, or None. 'Rolex 16610 ki price?' -> None (a model number).

    A price with a unit or currency wins; a bare number counts only with a budget word.
    """
    found = _find_prices(text)
    clear = [price for _, price, bare in found if not bare]
    if clear:
        return clear[0]
    if found and BUDGET_WORDS & set(_WORD.findall(text.lower())):
        return found[0][1]
    return None
