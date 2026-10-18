r"""Phase 12, Step 1: builds evals/product_qa.jsonl from the REAL dev store (read-only).

    set PYTHONPATH=src
    uv run python scripts\make_product_qa.py

Each line = one question with the exact fact the bot must give (price, stock, colours, sizes).
Run it again whenever the store data changes (price, stock, new colours), then commit the file.
Only products with ONE clear answer are used (all variants share the price, and so on).
"""

import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from app.integrations.shopify import gateway

OUT = Path("evals") / "product_qa.jsonl"
LANGS = ["en", "roman_ur", "mixed"]

# How many questions of each kind we want (blueprint asks for at least 60 in total).
# The dev store has few colour options and no size options yet, so price and stock carry more.
WANT = {"price": 32, "stock": 24, "colors": 16, "sizes": 10}

QUESTIONS = {
    "price": {
        "en": "How much is the {t}?",
        "roman_ur": "{t} ki price kya hai?",
        "mixed": "{t} ka price kitna hai please?",
    },
    "stock": {
        "en": "Is the {t} in stock?",
        "roman_ur": "{t} available hai?",
        "mixed": "Kya {t} stock mein hai?",
    },
    "colors": {
        "en": "Which colours does the {t} come in?",
        "roman_ur": "{t} kin kin rangon mein milti hai?",
        "mixed": "{t} kaun kaun se colours mein available hai?",
    },
    "sizes": {
        "en": "Which sizes does the {t} come in?",
        "roman_ur": "{t} kaun kaun se size mein milti hai?",
        "mixed": "{t} ke kaun se sizes available hain?",
    },
}


def plain(value: Any) -> str:
    """Enum or text -> plain text."""
    return str(getattr(value, "value", value))


# The bot answers about WATCHES only (anything else is out of scope by design). Products of the dev
# store that are not watches must not become golden questions. Add a word here if one appears.
NOT_WATCH_WORDS = ("cream", "bubble gum", "combo")


def is_watch(product: Any) -> bool:
    title = product.title.lower()
    return not any(word in title for word in NOT_WATCH_WORDS)


def clear_titles(products: list[Any]) -> list[Any]:
    """Keep products whose title is unique and not a part of another title.

    Example: 'Ladies Watch' is dropped when 'Ladies Watch Leather' exists (the bot would ask which).
    """
    products = [p for p in products if is_watch(p)]
    titles = [p.title.strip().lower() for p in products]
    keep = []
    for p, t in zip(products, titles, strict=True):
        if titles.count(t) > 1:
            continue
        if any(t != other and t in other for other in titles):
            continue
        keep.append(p)
    return sorted(keep, key=lambda p: p.title)


def option_values(p: Any, words: tuple[str, ...]) -> list[str]:
    for o in p.options:
        name = o.name.strip().lower()
        if any(w in name for w in words) and len(o.values) >= 2:
            return sorted(str(v) for v in o.values)
    return []


def facts(p: Any, kind: str) -> dict[str, Any] | None:
    """The exact expected fact for this kind, or None if the product has no single clear answer."""
    if kind == "price":
        prices = {(v.price.amount, v.price.currency) for v in p.variants}
        if len(prices) != 1:
            return None
        amount, currency = next(iter(prices))
        return {"amount": str(amount), "currency": str(currency)}
    if kind == "stock":
        states = {plain(v.stock.value) for v in p.variants}
        return {"stock": states.pop()} if len(states) == 1 else None
    if kind == "colors":
        values = option_values(p, ("color", "colour"))
        return {"values": values} if values else None
    values = option_values(p, ("size",))
    return {"values": values} if values else None


def pick(items: list[Any], want: int) -> list[Any]:
    """Spread the choice over the whole sorted list, not only the first products."""
    step = max(1, len(items) // want)
    return items[::step][:want]


async def main() -> None:
    products = clear_titles(await gateway.list_products())
    print(f"Visible products with a clear title: {len(products)}")
    rows: list[dict[str, Any]] = []
    today = date.today().isoformat()
    for kind, want in WANT.items():
        eligible = [(p, f) for p in products if (f := facts(p, kind)) is not None]
        chosen = pick(eligible, want)
        print(f"{kind}: {len(eligible)} possible, {len(chosen)} used (wanted {want})")
        for n, (p, expected) in enumerate(chosen, start=1):
            lang = LANGS[n % len(LANGS)]
            rows.append(
                {
                    "id": f"{kind}-{n:02d}",
                    "kind": kind,
                    "lang": lang,
                    "product": p.title,
                    "question": QUESTIONS[kind][lang].format(t=p.title),
                    "expected": expected,
                    "source": f"real-store {today}",
                }
            )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    print(f"\nWrote {len(rows)} questions to {OUT}")
    if len(rows) < 60:
        print("WARNING: fewer than 60 questions (blueprint 11.3).")
        print("Add colour/size options or products in the store, then run again.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    asyncio.run(main())
