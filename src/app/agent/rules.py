"""Rule-based question answering for `app.cli ask` (Phase 3, step 8). No LLM anywhere.

It reads one customer sentence (English or Roman Urdu), decides what is being asked with plain
word lists, finds the product with the search tool, and answers ONLY from tool results. The LLM
router and composer replace this in Phase 9; until then this proves the tools work end to end.

    parse_question(text)   pure: sentence -> Parsed (intent, product words, colour, size, budget)
    answer(session, text)  runs the tools and writes the reply (English only in Phase 3)
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import tools
from app.catalog.images import MAX_IMAGES
from app.catalog.normalizer import known_colour, normalize_text, parse_mm
from app.catalog.specs import SPEC_WORDS
from app.domain.models import ToolResult
from app.language.lexicon import get_lexicon
from app.language.price_parser import NUMBER_WORDS, parse_price


class Ask(StrEnum):
    SEARCH = "search"
    PRICE = "price"
    STOCK = "stock"
    OPTIONS = "options"
    SPECS = "specs"
    IMAGE = "image"
    OUT_OF_SCOPE = "out_of_scope"


# ---- word lists (lowercase, already in normalize_text form)

_IMAGE = {
    "photo", "photos", "pic", "pics", "picture", "pictures", "image", "images",
    "tasveer", "tasvir", "dikhao", "dikha", "dikhaen", "dikhayen",
}  # fmt: skip
_MORE = {"more", "aur", "zyada", "zyadah", "mazeed", "another", "others", "all"}
_SPEC: set[str] = (set().union(*SPEC_WORDS.values()) - {"size", "mm", "width"}) | {
    "spec", "specs", "specification", "specifications",
}  # 'size' alone means the size OPTIONS, 'case size' means the spec  # fmt: skip
_PRICE_EXPLICIT = {
    "price", "prices", "qeemat", "keemat", "kimat", "cost", "rate", "rupay", "rupees", "paisay",
    "much",
}  # fmt: skip
_PRICE_VAGUE = {"kitne", "kitna", "kitni", "kitny", "kitnay"}  # only if nothing else fits
_OPTIONS_PLURAL = {"colours", "colors", "sizes", "variants", "variant", "options", "rang"}
_OPTIONS_SINGLE = {"colour", "color", "size"}
_STOCK = {"available", "availability", "stock", "instock", "milegi", "milega", "milti"}
_OUT_OF_SCOPE = {
    "order", "orders", "parcel", "tracking", "track", "refund", "return", "payment", "address",
    "delivery", "cancel", "complaint",
}  # fmt: skip
_PRICE_BEFORE = {"under", "below", "within", "upto", "max", "maximum", "budget"}  # 'under 10k'
_PRICE_AFTER = {"kam", "tak", "andar"}  # '10k se kam', '10000 tak'
_FILLER = {
    "watch", "watches", "ghari", "ghadi", "show", "tell", "about", "this", "that", "it", "its",
    "please", "pls", "plz", "want", "need", "looking", "any", "some", "buy", "pass", "paas",
    "have", "has", "does", "come", "comes", "get", "give", "send",
}  # fmt: skip

_NUMBER_UNITS = {"k", "hazar", "hazaar", "lakh", "lac", "lacs", "thousand", "rs", "pkr"}
_NUMBERISH = re.compile(r"\d+(?:k|hazar|hazaar|lakh|lac|lacs|thousand)?")
_SIZE_MM = re.compile(r"(\d{2,3})mm")

OUT_OF_SCOPE_TEXT = (
    "I can answer product questions here: price, stock, colours, sizes, specs and pictures. "
    "For orders, delivery or refunds please contact the team."
)
NEED_PRODUCT_TEXT = "Which watch do you mean? Please tell me the name or model."


@dataclass(frozen=True)
class Parsed:
    intent: Ask
    product_query: str  # product words only ('ga 2100'); '' when the customer named none
    colour: str | None = None  # canonical English colour ('kala' -> 'black')
    size_mm: int | None = None
    max_price: int | None = None
    more_images: bool = False
    spec_text: str = ""  # the normalised sentence, for resolve_spec in the specs tool


@dataclass(frozen=True)
class Answer:
    text: str
    parsed: Parsed
    results: list[ToolResult]  # every tool call made, for --debug and for checking vs Shopify


# ---- parsing (pure)


def _numberish(token: str) -> bool:
    return bool(_NUMBERISH.fullmatch(token)) or token in _NUMBER_UNITS or token in NUMBER_WORDS


def _extract_price(tokens: list[str]) -> tuple[int | None, set[int]]:
    """A budget only after a cue ('under 10k', '10k se kam'): a model code is no price."""
    for i, token in enumerate(tokens):
        if token in _PRICE_BEFORE:
            end = i + 1
            while end < len(tokens) and end < i + 4 and _numberish(tokens[end]):
                end += 1
            span = list(range(i + 1, end))
        elif token in _PRICE_AFTER:
            last = i - 2 if i >= 2 and tokens[i - 1] == "se" else i - 1
            first = last
            while first >= 0 and first > last - 4 and _numberish(tokens[first]):
                first -= 1
            span = list(range(first + 1, last + 1))
        else:
            continue
        price = parse_price(" ".join(tokens[k] for k in span)) if span else None
        if price:
            return price, {i, *span}
    return None, set()


def _extract_size(tokens: list[str]) -> tuple[int | None, set[int]]:
    """'40mm' or '40 mm' -> 40, with the token positions to drop from the product words."""
    for i, token in enumerate(tokens):
        match = _SIZE_MM.fullmatch(token)
        if match and parse_mm(token):
            return int(match.group(1)), {i}
        if token == "mm" and i > 0 and tokens[i - 1].isdigit() and parse_mm(f"{tokens[i - 1]}mm"):
            return int(tokens[i - 1]), {i - 1, i}
    return None, set()


def parse_question(text: str) -> Parsed:
    normalised = normalize_text(text)
    tokens = normalised.split()
    words = set(tokens)
    if words & _OUT_OF_SCOPE:
        return Parsed(Ask.OUT_OF_SCOPE, "")

    price, price_at = _extract_price(tokens)
    size, size_at = _extract_size(tokens)
    colour = next((c for t in tokens if (c := known_colour(t))), None)

    if words & _IMAGE:
        intent = Ask.IMAGE
    elif words & _SPEC:
        intent = Ask.SPECS
    elif words & _PRICE_EXPLICIT:
        intent = Ask.PRICE
    elif words & _OPTIONS_PLURAL or (words & _OPTIONS_SINGLE and not (colour or size)):
        intent = Ask.OPTIONS
    elif words & _STOCK or colour or size:
        intent = Ask.STOCK
    elif words & _PRICE_VAGUE:
        intent = Ask.PRICE
    else:
        intent = Ask.SEARCH

    lexicon = get_lexicon()
    noise = (
        lexicon.roman_urdu_words | lexicon.english_stopwords | _FILLER | _IMAGE | _MORE | _SPEC
        | _PRICE_EXPLICIT | _PRICE_VAGUE | _OPTIONS_PLURAL | _OPTIONS_SINGLE | _STOCK
        | _PRICE_BEFORE | _PRICE_AFTER
    )  # fmt: skip
    dropped = price_at | size_at
    product = [
        t
        for i, t in enumerate(tokens)
        if i not in dropped and t not in noise and not known_colour(t)
    ]
    query = " ".join(product)

    if not query and (colour or size or price) and intent in (Ask.STOCK, Ask.SEARCH):
        intent, query = Ask.SEARCH, "watch"  # 'black watch under 10k': browse by the filters

    return Parsed(
        intent=intent,
        product_query=query,
        colour=colour,
        size_mm=size,
        max_price=price,
        more_images=bool(words & _MORE) and intent is Ask.IMAGE,
        spec_text=normalised[: tools.MAX_QUERY_LEN],
    )


# ---- answering


def _data(result: ToolResult) -> dict[str, Any]:
    return result.data if isinstance(result.data, dict) else {}


def _unavailable(result: ToolResult) -> str:
    stock = _data(result).get("stock")
    if stock:
        return str(stock["message"])  # 'I cannot check stock right now.'
    return "I cannot check that right now. Please try again in a moment."


def _failed(result: ToolResult) -> str:
    if result.error_code == "shopify_unavailable":
        return _unavailable(result)
    return "I could not find that product."


def _name(data: dict[str, Any]) -> str:
    variant = data.get("variant_title")
    title = data.get("title", "")
    return f"{title} ({variant})" if variant and variant != "Default Title" else title


def _colour_note(data: dict[str, Any], parsed: Parsed) -> str:
    if data.get("colour_unverified") and parsed.colour:
        return (
            " Note: the colours of this model are not listed,"
            f" so I cannot confirm {parsed.colour}."
        )
    return ""


def _not_one(data: dict[str, Any]) -> str | None:
    """Text for 'several variants fit' and 'not offered'; None when exactly one variant fits."""
    if data.get("match") == "ambiguous":
        options = "; ".join(v["title"] for v in data["variants"])
        return f"{data['title']} comes in: {options}. Which one do you mean?"
    if data.get("match") == "none":
        offered = data.get("colours_offered") or []
        extra = f" Colours offered: {', '.join(offered)}." if offered else ""
        return f"{data['message']}{extra}"
    return None


def _price_text(result: ToolResult, parsed: Parsed) -> str:
    if not result.ok:
        return _failed(result)
    data = _data(result)
    other = _not_one(data)
    if other:
        return other
    price = data["price"]
    sale = f" (was {price['compare_at_display']})" if price.get("compare_at_display") else ""
    return f"{_name(data)}: {price['display']}{sale}.{_colour_note(data, parsed)}"


def _stock_text(result: ToolResult, parsed: Parsed) -> str:
    if not result.ok:
        return _failed(result)
    data = _data(result)
    other = _not_one(data)
    if other:
        return other
    stock = data["stock"]
    count = f" ({stock['quantity']} left)" if stock.get("quantity") else ""
    return f"{_name(data)}: {stock['message']}{count}{_colour_note(data, parsed)}"


def _options_text(result: ToolResult) -> str:
    if not result.ok:
        return _failed(result)
    data = _data(result)
    colours = (
        f"Colours: {', '.join(data['colours'])}."
        if data["colours"]
        else "The colours of this model are not listed."
    )
    sizes = (
        f"Case sizes: {', '.join(f'{s} mm' for s in data['sizes_mm'])}."
        if data["sizes_mm"]
        else "The case sizes are not listed."
    )
    return f"{data['title']}: {colours} {sizes} (These are offered options, not stock.)"


def _specs_text(result: ToolResult) -> str:
    if not result.ok:
        return _failed(result)
    data = _data(result)
    if not data["available"]:
        return f"{data['title']}: {data['message']}"
    parts = "; ".join(f"{s['name'].replace('_', ' ')}: {s['value']}" for s in data["specs"])
    return f"{data['title']}: {parts}."


def _image_text(result: ToolResult, parsed: Parsed) -> str:
    if not result.ok:
        return _failed(result)
    data = _data(result)
    if not data["images"]:
        return f"{data['message']} {data['product_url']}"
    urls = " ".join(image["url"] for image in data["images"])
    missing = (
        f" I do not have a {parsed.colour} picture, so this is the main one."
        if parsed.colour and data.get("colour_image_found") is False
        else ""
    )
    only = (
        " That is the only picture I have for this product."
        if parsed.more_images and len(data["images"]) == 1
        else ""
    )
    return f"{data['title']}:{missing} {urls}{only}"


def _search_text(data: dict[str, Any]) -> str:
    candidates = data["candidates"]
    if data["kind"] == "none":
        if data["name_matches"]:
            return "I found watches with that name, but none matches that colour, size or budget."
        return "I could not find a product like that."
    if data["kind"] == "ambiguous":
        names = "; ".join(c["title"] for c in candidates)
        return f"I found a few close matches: {names}. Which one do you mean?"
    best = candidates[0]
    note = " I cannot confirm its colour." if best["colour_verified"] is False else ""
    return f"I found {best['title']}.{note} {best['url']}"


async def answer(session: AsyncSession, text: str) -> Answer:
    parsed = parse_question(text)
    results: list[ToolResult] = []
    if parsed.intent is Ask.OUT_OF_SCOPE:
        return Answer(OUT_OF_SCOPE_TEXT, parsed, results)
    if not parsed.product_query:
        return Answer(NEED_PRODUCT_TEXT, parsed, results)

    # Only a plain search applies colour, size and budget as filters. For price, stock and the
    # rest the product is found by name, and colour or size are checked on its variants, so
    # 'no red one' stays different from 'product not found'.
    browsing = parsed.intent is Ask.SEARCH
    found = await tools.tool_search_products(
        session,
        parsed.product_query,
        colour=parsed.colour if browsing else None,
        size_mm=parsed.size_mm if browsing else None,
        max_price=parsed.max_price if browsing else None,
    )
    results.append(found)
    data = _data(found)
    if not found.ok:
        return Answer("I could not understand that. Please rephrase.", parsed, results)
    if browsing or data["kind"] != "one":
        return Answer(_search_text(data), parsed, results)

    product_id = data["candidates"][0]["product_id"]
    if parsed.intent is Ask.PRICE:
        result = await tools.tool_get_price(
            session, product_id, colour=parsed.colour, size_mm=parsed.size_mm
        )
        reply = _price_text(result, parsed)
    elif parsed.intent is Ask.STOCK:
        result = await tools.tool_check_stock(
            session, product_id, colour=parsed.colour, size_mm=parsed.size_mm
        )
        reply = _stock_text(result, parsed)
    elif parsed.intent is Ask.OPTIONS:
        result = await tools.tool_get_colors_sizes(session, product_id)
        reply = _options_text(result)
    elif parsed.intent is Ask.SPECS:
        result = await tools.tool_get_specs(session, product_id, parsed.spec_text)
        reply = _specs_text(result)
    else:  # Ask.IMAGE
        result = await tools.tool_get_product_image(
            session,
            product_id,
            colour=parsed.colour,
            max_images=MAX_IMAGES if parsed.more_images else 1,
        )
        reply = _image_text(result, parsed)
    results.append(result)
    return Answer(reply, parsed, results)
