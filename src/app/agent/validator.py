"""Phase 9, step 6: the output validator (Blueprint 8.7). Plain Python, no model, no network.

    problems = validate(response, ValidationContext(...))      # [] means: the reply may be sent

It runs on every composed reply before it enters the outbox. Each problem is ONE line that starts
with a code (length, numbers, price, url, colour, id, language, leak, private_data). The lines go
back to the composer as <validator_feedback> for the one retry, and the codes are counted in the
log. A problem line never contains a secret or a piece of the system prompt.

Checks (8.7):
  length        not too long, at most MAX_SENTENCES sentences, at most one question
  numbers       every number is in the tool results, the passages or the customer's message
  price         every 'Rs ...' amount is a price from get_price (or a fee in a policy passage)
  url           only the store domain, or a URL that a tool returned
  colour        only colours of the product being discussed (title or options)
  id            product / variant ids and the image URL come from tool results
  language      the reply is in the reply language (language/policy.py reply_matches)
  leak          no tool name, internal id, prompt text, secret, e-mail or phone number
  private_data  a private_data_request reply has no order or customer data

Known limits (on purpose, it is a cheap safety net and not a reader): it cannot tell that
"yes, blue is available" is false when the colour is allowed to be named, and a model name with
letters glued to digits ("GA2100") is checked only as a number.
"""

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from app.agent.composer import PROMPT_PATH as COMPOSER_PROMPT_PATH
from app.agent.router import PROMPT_PATH as ROUTER_PROMPT_PATH
from app.agent.tools import TOOL_POLICY, TOOL_PRICE, TOOLS
from app.catalog.normalizer import colour_of_value, known_colour, normalize_text
from app.domain.models import BotResponse, Intent, ToolResult
from app.language.policy import ReplyLang, reply_matches

TOOL_HANDOFF_NAME = "create_handoff"

MAX_REPLY_CHARS = 700  # BotResponse allows 1000; the prompt asks for 4 short sentences
MAX_SENTENCES = 5  # the prompt says 4; one more is tolerated so the validator is not too strict
MAX_QUESTIONS = 1
MAX_QUESTIONS_BILINGUAL = 2  # en_roman_ur: one question per language
LEAK_NGRAM = 6  # six words in a row copied from a prompt are a leak

_NUMBER = re.compile(r"(?<![\d,.])\d[\d,]*(?:\.\d+)?")
_MONEY = re.compile(
    r"(?:\bRs\.?|\bPKR\b|₨)\s*(\d[\d,]*(?:\.\d+)?)"
    r"|(\d[\d,]*(?:\.\d+)?)\s*(?:rupees?|rupay|rupaye|rs\b|pkr\b)",
    re.IGNORECASE,
)
_URL = re.compile(
    r"(?:https?://|www\.)[^\s<>\"')\]]+"
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:com|pk|net|org|io|co|shop|store|app)\b(?:/[^\s<>\"')\]]*)?",
    re.IGNORECASE,
)
_GID = re.compile(r"gid://shopify/[A-Za-z]+/\d+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<![\w.,])\+?\d(?:[ -]?\d){9,}(?!\d)")
_TOKEN = re.compile(r"\b(?:sk|gsk|shpat|shpss|EAA)[-_A-Za-z0-9]{16,}|\b[A-Za-z0-9_-]{40,}\b")
_ORDER_REF = re.compile(r"#\s*\d{3,}|\d{4,}")
_WORD = re.compile(r"[A-Za-z]+")

_COLOUR_KEYS = {"colour", "colours", "colours_offered"}
_TITLE_KEYS = {"title", "variant_title"}


@dataclass(frozen=True)
class ValidationContext:
    """What the validator needs besides the reply. The caller (validate_node) fills it."""

    reply_lang: ReplyLang
    intent: Intent
    results: Sequence[ToolResult]
    customer_text: str = ""
    store_domain: str = ""  # SHOPIFY_SHOP_DOMAIN
    secrets: Sequence[str] = field(default_factory=tuple)  # literal secret values to look for


# ---- helpers


def _norm_number(raw: str) -> str | None:
    try:
        value = Decimal(raw.rstrip(",").replace(",", ""))
    except InvalidOperation:
        return None
    return format(value.normalize(), "f")  # '12,500', '12500.00' and '12500' all become '12500'


def _numbers_in(text: str) -> set[str]:
    found = (_norm_number(m.group(0)) for m in _NUMBER.finditer(text))
    return {n for n in found if n is not None}


def _money_in(text: str) -> set[str]:
    found = (_norm_number(m.group(1) or m.group(2)) for m in _MONEY.finditer(text))
    return {n for n in found if n is not None}


def _walk(data: Any, key: str = "") -> Iterator[tuple[str, Any]]:
    """Every (key, value) in nested dicts and lists; list items keep the key of their list."""
    if isinstance(data, dict):
        for k, v in data.items():
            yield from _walk(v, str(k))
    elif isinstance(data, list):
        for item in data:
            yield from _walk(item, key)
    else:
        yield key, data


def _host(url: str) -> str:
    candidate = url if "://" in url else "http://" + url
    try:
        host = urlsplit(candidate).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/.,;:!?)").lower()


def _colours_in_text(text: str) -> set[str]:
    return {c for w in _WORD.findall(text) if (c := known_colour(w))}


def _without_urls(text: str) -> str:
    return _URL.sub(" ", text)


@lru_cache
def _prompt_grams() -> frozenset[tuple[str, ...]]:
    grams: set[tuple[str, ...]] = set()
    for path in (COMPOSER_PROMPT_PATH, ROUTER_PROMPT_PATH):
        try:
            words = normalize_text(path.read_text(encoding="utf-8")).split()
        except OSError:
            continue
        grams.update(zip(*(words[i:] for i in range(LEAK_NGRAM)), strict=False))
    return frozenset(grams)


# ---- what the tools said (the only facts a reply may use)


@dataclass
class _Facts:
    text: str = ""  # all tool data as JSON (passages included)
    numbers: set[str] = field(default_factory=set)
    prices: set[str] = field(default_factory=set)  # from get_price, plus fees in policy passages
    urls: set[str] = field(default_factory=set)  # exact URLs a tool returned
    ids: set[str] = field(default_factory=set)
    colours: set[str] = field(default_factory=set)
    colours_loose: bool = False  # a colour was asked for that the data cannot confirm
    data_tools: int = 0  # tool results that are not a handoff record


def _build_facts(results: Sequence[ToolResult]) -> _Facts:
    facts = _Facts()
    chunks: list[str] = []
    for result in results:
        if result.tool == TOOL_HANDOFF_NAME or result.data is None:
            continue
        facts.data_tools += 1
        dumped = json.dumps(result.data, ensure_ascii=False, default=str)
        chunks.append(dumped)

        if result.tool == TOOL_POLICY:
            facts.prices |= _numbers_in(dumped)  # a delivery fee lives in a passage
        if result.tool == TOOL_PRICE and isinstance(result.data, dict):
            price = result.data.get("price")
            if isinstance(price, dict):
                for key in ("amount", "display", "compare_at_display"):
                    facts.prices |= _numbers_in(str(price.get(key) or ""))

        if isinstance(result.data, dict):
            data = result.data
            candidates = data.get("candidates") or []
            unverified = any(
                isinstance(c, dict) and c.get("colour_verified") is False for c in candidates
            )
            facts.colours_loose = facts.colours_loose or bool(
                data.get("match") == "none"
                or data.get("colour_unverified")
                or data.get("colours_listed") is False
                or unverified
            )
        for key, value in _walk(result.data):
            if not isinstance(value, str):
                continue
            if value.lower().startswith(("http://", "https://")):
                facts.urls.add(_norm_url(value))
            if key in _COLOUR_KEYS and (colour := colour_of_value(value)):
                facts.colours.add(colour)
            if key in _TITLE_KEYS:
                facts.colours |= _colours_in_text(value)

    facts.text = "\n".join(chunks)
    facts.numbers = _numbers_in(facts.text)
    facts.ids = set(_GID.findall(facts.text))
    return facts


# ---- the checks. Each returns a list of problem lines (empty = fine).


def _check_length(response: BotResponse, ctx: ValidationContext) -> list[str]:
    text = response.response_text
    problems: list[str] = []
    if len(text) > MAX_REPLY_CHARS:
        problems.append(f"length: the reply is too long ({len(text)} characters); be shorter")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.replace("Rs.", "Rs")) if s.strip()]
    if len(sentences) > MAX_SENTENCES:
        problems.append("length: use at most 4 short sentences")
    limit = MAX_QUESTIONS_BILINGUAL if ctx.reply_lang == "en_roman_ur" else MAX_QUESTIONS
    if text.count("?") > limit:
        problems.append("length: ask at most one question")
    return problems


def _check_numbers(response: BotResponse, ctx: ValidationContext, facts: _Facts) -> list[str]:
    text = _without_urls(response.response_text)
    problems: list[str] = []
    money = _money_in(text)
    for amount in sorted(money - facts.prices):
        problems.append(
            f"price: {amount} is not a price from the price tool; "
            "use only a price from the facts or say you cannot confirm it"
        )
    allowed = facts.numbers | _numbers_in(ctx.customer_text)
    for number in sorted((_numbers_in(text) - money) - allowed):
        problems.append(
            f"numbers: {number} is not in the facts; remove it or say you cannot confirm"
        )
    return problems


def _check_urls(response: BotResponse, ctx: ValidationContext, facts: _Facts) -> list[str]:
    store = _host(ctx.store_domain) if ctx.store_domain else ""
    problems: list[str] = []
    for match in _URL.finditer(response.response_text):
        url = match.group(0)
        if _norm_url(url) in facts.urls or (store and _host(url) == store):
            continue
        problems.append("url: a link is not from the store or a tool result; remove it")
        break
    return problems


def _check_colours(response: BotResponse, ctx: ValidationContext, facts: _Facts) -> list[str]:
    allowed = set(facts.colours)
    if facts.colours_loose:  # "we do not have blue" may name the colour the customer asked for
        allowed |= _colours_in_text(ctx.customer_text)
    wrong = sorted(_colours_in_text(response.response_text) - allowed)
    if not wrong:
        return []
    return [f"colour: {', '.join(wrong)} is not a colour of this product in the facts; remove it"]


def _check_ids(response: BotResponse, facts: _Facts) -> list[str]:
    problems: list[str] = []
    ids = list(response.product_ids)
    image = response.image
    if image is not None:
        ids.append(image.product_id)
        if image.variant_id:
            ids.append(image.variant_id)
        if _norm_url(image.image_url) not in facts.urls:
            problems.append("id: the image URL is not from a tool result")
    if any(i not in facts.ids for i in ids):
        problems.append("id: an id is not from the tool results; copy ids exactly")
    return problems


def _check_language(response: BotResponse, ctx: ValidationContext) -> list[str]:
    if reply_matches(response.response_text, ctx.reply_lang):
        return []
    return [f"language: write the reply in {ctx.reply_lang}"]


def _check_leaks(response: BotResponse, ctx: ValidationContext) -> list[str]:
    text = response.response_text
    plain = _without_urls(text)
    lowered = text.lower()
    leaked = False

    names = [*TOOLS, TOOL_HANDOFF_NAME]
    leaked |= any(re.search(rf"(?<![a-z_]){re.escape(n)}(?![a-z_])", lowered) for n in names)
    leaked |= bool(_GID.search(text))
    leaked |= bool(_TOKEN.search(plain)) or bool(_EMAIL.search(text))
    leaked |= bool(_PHONE.search(plain))
    leaked |= any(len(s) >= 8 and s in text for s in ctx.secrets)

    words = normalize_text(text).split()
    grams = _prompt_grams()
    leaked |= any(tuple(words[i : i + LEAK_NGRAM]) in grams for i in range(len(words)))

    if leaked:
        return [
            "leak: the reply contains internal details (tool names, ids, instructions, "
            "keys, e-mail or phone numbers); write only a normal customer answer"
        ]
    return []


def _check_private(response: BotResponse, ctx: ValidationContext, facts: _Facts) -> list[str]:
    if ctx.intent is not Intent.PRIVATE_DATA_REQUEST:
        return []
    plain = _without_urls(response.response_text)
    if facts.data_tools or _ORDER_REF.search(plain) or _EMAIL.search(response.response_text):
        return ["private_data: do not share order or customer data; say the team can help"]
    return []


# ---- the one entry point


def validate(response: BotResponse, ctx: ValidationContext) -> list[str]:
    """All problems with a composed reply. An empty list means it may be sent."""
    facts = _build_facts(ctx.results)
    problems: list[str] = []
    problems += _check_length(response, ctx)
    problems += _check_numbers(response, ctx, facts)
    problems += _check_urls(response, ctx, facts)
    problems += _check_colours(response, ctx, facts)
    problems += _check_ids(response, facts)
    problems += _check_language(response, ctx)
    problems += _check_leaks(response, ctx)
    problems += _check_private(response, ctx, facts)
    return problems
