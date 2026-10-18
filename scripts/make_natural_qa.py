r"""Phase 12, Step 3b: builds evals/natural_qa.jsonl, questions the way customers really write.

    set PYTHONPATH=src
    uv run python scripts\make_natural_qa.py

evals/product_qa.jsonl (the golden set) copies the exact product title, so it only proves the bot
works when a customer does the same. This set takes the same real products and writes the question
the messy way: half a title, no capital letters, a typo, Urdu script, filler words, or a follow-up
('iska price kya hai?' after the product was named).

The expected fact is the same exact fact as in the golden set (price, stock). The scorer
(evaluators.grade_natural) accepts that fact OR an honest question; only a WRONG fact fails.
The file is built once and committed, so every run asks the same questions. Re-run it only when
the store data changes. Needs the real dev store (read-only).
"""

import asyncio
import json
import random
import re
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from make_product_qa import clear_titles, facts, pick

from app.integrations.shopify import gateway

OUT = Path("evals") / "natural_qa.jsonl"
PER_KIND = 3  # questions of each kind (price, stock) per style: 7 styles x 2 x 3 = 42 rows
SHORT_WORDS = 3


def clean(title: str) -> str:
    """'PP - Nautilus Silver Brown - AAA Quality' -> 'pp nautilus silver brown aaa quality'."""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def without_suffix(title: str) -> str | None:
    """Drop the last ' - ...' part ('- AAA Quality'). None if the title has no such part."""
    parts = title.split(" - ")
    return " - ".join(parts[:-1]) if len(parts) >= 3 else None


def with_typo(title: str) -> str | None:
    """Swap two letters inside one longer word. The same title always gets the same typo."""
    rng = random.Random(title)
    words = [w for w in title.split() if len(w) >= 5 and w.isalpha()]
    if not words:
        return None
    word = rng.choice(words)
    i = rng.randrange(1, len(word) - 2)
    typo = word[:i] + word[i + 1] + word[i] + word[i + 2 :]
    return title.replace(word, typo, 1) if typo != word else None


def short_name(title: str) -> str | None:
    words = clean(title).split()
    return " ".join(words[:SHORT_WORDS]) if len(words) > SHORT_WORDS else None


# A builder gets (kind, title) and returns (setup messages, question), or None when this title
# does not suit the style.
Built = tuple[list[str], str]
Builder = Callable[[str, str], Built | None]


def b_partial(kind: str, title: str) -> Built | None:
    name = without_suffix(title)
    if name is None:
        return None
    return [], (f"How much is the {name}?" if kind == "price" else f"Is the {name} in stock?")


def b_lower_ur(kind: str, title: str) -> Built | None:
    name = clean(title)
    return [], (f"{name} ka rate kya hai" if kind == "price" else f"{name} stock mein hai kya")


def b_typo(kind: str, title: str) -> Built | None:
    name = with_typo(title)
    if name is None:
        return None
    return [], (f"price of {name}?" if kind == "price" else f"is {name} available?")


def b_short(kind: str, title: str) -> Built | None:
    name = short_name(title)
    if name is None:
        return None
    return [], (f"{name} price?" if kind == "price" else f"{name} available hai?")


def b_filler(kind: str, title: str) -> Built | None:
    if kind == "price":
        return [], f"bhai {title} kitne ka hai, jaldi batao"
    return [], f"salam, kya {title} stock mein hai? zaroori hai"


def b_followup(kind: str, title: str) -> Built | None:
    setup = [f"Mujhe {title} ke baare mein poochna hai"]
    return setup, ("iska price kya hai?" if kind == "price" else "ye available hai?")


def b_ur_script(kind: str, title: str) -> Built | None:
    return [], (f"{title} کی قیمت کیا ہے؟" if kind == "price" else f"کیا {title} اسٹاک میں ہے؟")


# style -> (language label, builder)
STYLES: dict[str, tuple[str, Builder]] = {
    "partial": ("en", b_partial),
    "lower_ur": ("roman_ur", b_lower_ur),
    "typo": ("en", b_typo),
    "short": ("mixed", b_short),
    "filler": ("mixed", b_filler),
    "followup": ("mixed", b_followup),
    "ur_script": ("ur", b_ur_script),
}


async def main() -> None:
    products = clear_titles(await gateway.list_products())
    print(f"Watches with a clear title: {len(products)}")
    rows: list[dict[str, Any]] = []
    today = date.today().isoformat()
    for style_no, (style, (lang, build)) in enumerate(STYLES.items()):
        for kind in ("price", "stock"):
            eligible: list[tuple[Any, dict[str, Any], Built]] = []
            for p in products:
                expected = facts(p, kind)
                built = build(kind, p.title)
                if expected is not None and built is not None:
                    eligible.append((p, expected, built))
            # a different slice of the store for every style, so the products vary
            shift = (style_no * PER_KIND) % max(1, len(eligible))
            eligible = eligible[shift:] + eligible[:shift]
            for p, expected, (setup, question) in pick(eligible, PER_KIND):
                rows.append(
                    {
                        "id": f"nat-{style}-{kind}-{len(rows) + 1:02d}",
                        "kind": kind,
                        "style": style,
                        "lang": lang,
                        "product": p.title,
                        "setup": setup,
                        "question": question,
                        "expected": expected,
                        "source": f"real-store {today}",
                    }
                )
        print(f"{style}: {sum(r['style'] == style for r in rows)} questions")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    print(f"\nWrote {len(rows)} questions to {OUT}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    asyncio.run(main())
