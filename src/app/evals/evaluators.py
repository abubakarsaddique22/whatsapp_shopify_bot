"""Exact-fact evaluators (Phase 12, Step 2; Blueprint 11.3: 'facts are scored by exact match').

Each check gets the bot's reply text and the expected fact from evals/product_qa.jsonl and says
ok or not. No model is involved: the result is the same every time. Plain functions only.

- price:  the expected amount must be one of the amounts in the reply ('Rs 21,000' = '21000.00').
          Numbers inside a product name (GA-2100) are ignored when the reply has 'Rs' / 'PKR'.
- stock:  the reply is read as one stock state (in_stock, low_stock, ...) and must match.
- colors / sizes: every expected value must appear in the reply, and none of the 'forbidden'
          values (colours or sizes this product does NOT have) may appear.
"""

import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel


class Check(BaseModel):
    ok: bool
    reason: str = ""


# ---- price

_NUMBER = r"\d[\d,]*(?:\.\d+)?"
_RS_BEFORE = re.compile(rf"(?:\brs\.?|\bpkr)\s*({_NUMBER})", re.IGNORECASE)
_RS_AFTER = re.compile(rf"({_NUMBER})\s*(?:rs\b|rupees|rupay|rupaye|pkr|/-)", re.IGNORECASE)
_ANY_NUMBER = re.compile(_NUMBER)


def _to_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def amounts_in(reply: str) -> list[Decimal]:
    """Prices in a reply. Numbers next to Rs / PKR / rupay win; if there are none, any number."""
    found = _RS_BEFORE.findall(reply) + _RS_AFTER.findall(reply)
    if not found:
        found = _ANY_NUMBER.findall(reply)
    return [d for d in (_to_decimal(f) for f in found) if d is not None]


def check_price(reply: str, expected: Mapping[str, Any]) -> Check:
    want = Decimal(str(expected["amount"]))
    got = amounts_in(reply)
    if want in got:
        return Check(ok=True)
    return Check(ok=False, reason=f"expected price {want}, reply has {[str(g) for g in got]}")


# ---- stock

# Order matters: 'not available' must be read as OUT before 'available' is read as IN.
_OUT = (
    "out of stock", "sold out", "not in stock", "not available", "unavailable", "khatam",
    "stock mein nahi", "stock main nahi", "available nahi", "maujood nahi", "mojood nahi",
    "stock nahi",
)
_LOW = ("limited", "low stock", "few left", "only a few", "kam stock")
_BACKORDER = ("backorder", "back order", "take longer", "delivery may")
_IN = (
    "in stock", "available", "maujood", "mojood", "stock mein hai", "stock main hai",
    "mil jaye", "mil jayegi", "mil jaegi",
)


def stock_state_of(reply: str) -> str:
    """Which stock state does this reply say? 'unknown' when it says nothing clear."""
    text = reply.lower()
    for state, words in (
        ("out_of_stock", _OUT),
        ("low_stock", _LOW),
        ("backorder", _BACKORDER),
        ("in_stock", _IN),
    ):
        if any(w in text for w in words):
            return state
    return "unknown"


def check_stock(reply: str, expected: Mapping[str, Any]) -> Check:
    want = str(expected["stock"])
    got = stock_state_of(reply)
    if got == want:
        return Check(ok=True)
    return Check(ok=False, reason=f"expected stock {want}, reply says {got}")


# ---- colours and sizes


def _has_word(text: str, value: str) -> bool:
    """True if `value` is in `text` as a whole word ('red' is not found inside 'colored')."""
    return re.search(rf"(?<!\w){re.escape(value.lower())}(?!\w)", text.lower()) is not None


def _same_letters(text: str, value: str) -> bool:
    """A pair written another way: 'BLACK&ROSEGOLD' is in 'Black & Rose Gold'.

    Only for values made of several parts. For one word it would find 'red' inside 'colored'.
    """
    parts = [p for p in re.split(r"[^a-z0-9]+", value.lower()) if p]
    return len(parts) > 1 and "".join(parts) in "".join(re.findall(r"[a-z0-9]+", text.lower()))


def check_values(
    reply: str, expected: Mapping[str, Any], forbidden: Iterable[str] = ()
) -> Check:
    values = [str(v) for v in expected["values"]]
    # A merchant's option can be a pair ('Gold & Black', 'Green-Black'). A reply that names every
    # part of it ('gold ... black') says the same thing, so the parts count as the value.
    pieces = {v: [p for p in re.split(r"[^A-Za-z0-9]+", v) if p] for v in values}
    missing = [
        v
        for v in values
        if not _has_word(reply, v)
        and not all(_has_word(reply, p) for p in pieces[v])
        and not _same_letters(reply, v)
    ]
    own = {v.lower() for v in values}
    own_words = {p.lower() for ps in pieces.values() for p in ps}  # 'black' in 'Gold & Black'
    wrong = [
        f
        for f in forbidden
        if f.lower() not in own and f.lower() not in own_words and _has_word(reply, f)
    ]
    if not missing and not wrong:
        return Check(ok=True)
    parts = []
    if missing:
        parts.append(f"missing {missing}")
    if wrong:
        parts.append(f"not offered but mentioned {wrong}")
    return Check(ok=False, reason="; ".join(parts))


# ---- one row of evals/product_qa.jsonl


def check_row(row: Mapping[str, Any], reply: str, forbidden: Iterable[str] = ()) -> Check:
    kind = row["kind"]
    expected = row["expected"]
    if kind == "price":
        return check_price(reply, expected)
    if kind == "stock":
        return check_stock(reply, expected)
    if kind in ("colors", "sizes"):
        return check_values(reply, expected, forbidden)
    return Check(ok=False, reason=f"unknown kind {kind!r}")


# ---- natural questions (evals/natural_qa.jsonl): a right fact or an honest question


class Verdict(BaseModel):
    verdict: str  # correct | clarified | wrong | error
    reason: str = ""


def grade_natural(row: Mapping[str, Any], reply: str) -> Verdict:
    """Grade a reply to a natural (messy) question.

    correct:   the reply gives the expected fact.
    clarified: the reply gives NO fact (it asks which product, offers a person, says it cannot
               confirm). This is allowed: asking is better than guessing.
    wrong:     the reply states a fact that is not the expected one. This must never happen.
    error:     the run itself failed (provider down, crash), nothing to judge.

    Prices are read only next to Rs / PKR / rupees, so a number inside a product name or a
    question ('which of these 3?') is not taken as a price.
    """
    if reply.startswith(("[ERROR", "[PROVIDER FAILED")):
        return Verdict(verdict="error", reason=reply[:120])
    kind = row["kind"]
    expected = row["expected"]
    if kind == "price":
        want = Decimal(str(expected["amount"]))
        got = [
            d
            for d in (_to_decimal(f) for f in _RS_BEFORE.findall(reply) + _RS_AFTER.findall(reply))
            if d is not None
        ]
        if not got:
            return Verdict(verdict="clarified")
        if want in got:
            return Verdict(verdict="correct")
        return Verdict(
            verdict="wrong", reason=f"expected price {want}, reply says {[str(g) for g in got]}"
        )
    if kind == "stock":
        want_state = str(expected["stock"])
        state = stock_state_of(reply)
        if state == "unknown":
            return Verdict(verdict="clarified")
        if state == want_state:
            return Verdict(verdict="correct")
        return Verdict(verdict="wrong", reason=f"expected stock {want_state}, reply says {state}")
    return Verdict(verdict="error", reason=f"unknown kind {kind!r}")
