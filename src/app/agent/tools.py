"""The six validated product tools (Phase 3, step 7).

Each tool takes an AsyncSession plus validated arguments and returns a `ToolResult`
(domain/models.py). Rules that hold for every tool:

* Ids may come from an LLM, so they are checked first (Shopify global ids only).
* The mirror is used to FIND things (search, options, specs, pictures). A price or a stock
  state a customer sees always comes from a live Shopify call (catalog/stock.py).
* A hidden or unknown product is `not_found`: the two look exactly the same.
* If Shopify cannot answer, the result says so (`shopify_unavailable`, stock state UNKNOWN).
  Nothing is guessed and nothing is read from an old copy.
* Tools never raise for expected problems: they return `ok=False` with an `error_code`.

Tools: search_products, get_price, check_stock, get_colors_sizes, get_specs, get_product_image.

Phase 8 adds a seventh tool, search_policy: approved passages from the knowledge base (refund,
shipping, privacy, FAQ, pages). It returns passages with source ids, or an abstention when
nothing is relevant enough. It reads only kb_documents and kb_chunks, never Shopify.
"""

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import stock as stock_mod
from app.catalog.images import get_images
from app.catalog.normalizer import colour_of_value, colours_of
from app.catalog.price import price_info
from app.catalog.search import (
    VariantRow,
    search_products,
    variant_matches,
    variant_size_mm,
)
from app.catalog.specs import get_specs
from app.core.errors import EmbeddingError, ShopifyError, ValidationFailed
from app.core.settings import get_settings
from app.db import repositories as repo
from app.domain.catalog import StockState
from app.domain.models import ToolResult
from app.integrations.embeddings.base import Embedder
from app.integrations.embeddings.factory import get_embedder
from app.rag.retrieve import DOC_TYPES, retrieve

TOOL_SEARCH = "search_products"
TOOL_PRICE = "get_price"
TOOL_STOCK = "check_stock"
TOOL_OPTIONS = "get_colors_sizes"
TOOL_SPECS = "get_specs"
TOOL_IMAGE = "get_product_image"
TOOL_POLICY = "search_policy"

MAX_QUERY_LEN = 120  # same limit as RouterOutput.product_query
MAX_KB_QUERY_LEN = 200  # same limit as RouterOutput.kb_query_en
MAX_AMBIGUOUS_VARIANTS = 10
MAX_OPTION_VARIANTS = 20

NOT_OFFERED_MESSAGES = {
    "colour_not_offered": stock_mod.STOCK_MESSAGES[StockState.COLOUR_NOT_OFFERED],
    "colour_unknown": "I could not confirm that colour for this model.",
    "size_not_offered": "This model does not come in that size.",
    "combination_not_offered": "This model does not come in that colour and size together.",
}

_PRODUCT_ID = re.compile(r"gid://shopify/Product/\d+")
_VARIANT_ID = re.compile(r"gid://shopify/ProductVariant/\d+")


# ---- small helpers


def _result(
    tool: str,
    *,
    ok: bool = True,
    data: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> ToolResult:
    return ToolResult(
        tool=tool,
        ok=ok,
        data=data,
        error_code=error_code,
        source_id=f"{tool}:{uuid.uuid4().hex[:12]}",
        fetched_at=datetime.now(UTC),
    )


def _product_id(value: str) -> str:
    if not _PRODUCT_ID.fullmatch(value or ""):
        raise ValidationFailed("Not a valid Shopify Product id")
    return value


def _variant_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not _VARIANT_ID.fullmatch(value):
        raise ValidationFailed("Not a valid Shopify ProductVariant id")
    return value


def _check_filters(size_mm: int | None, max_price: int | None = None) -> None:
    """Same bounds as RouterOutput, so a bad number never reaches the database."""
    if size_mm is not None and not 10 <= size_mm <= 80:
        raise ValidationFailed("size_mm must be between 10 and 80")
    if max_price is not None and max_price < 0:
        raise ValidationFailed("max_price cannot be negative")


async def _guard(tool: str, work: Awaitable[ToolResult]) -> ToolResult:
    """Turn the expected failures into a ToolResult instead of an exception."""
    try:
        return await work
    except ValidationFailed:
        return _result(tool, ok=False, error_code="invalid_input")
    except ShopifyError:  # unavailable, throttled, auth: the customer sees the same honest answer
        return _result(tool, ok=False, error_code="shopify_unavailable")


def _as_variant_row(row: Row[Any]) -> VariantRow:
    """Mirror row -> the shape search.variant_matches wants (the price is not used here)."""
    return VariantRow(row.id, row.title, row.options or {}, Decimal(0))


# ---- choosing ONE variant (shared by get_price and check_stock)


@dataclass(frozen=True)
class Resolution:
    variant: Row[Any] | None = None  # the one variant to look up live
    title: str = ""  # product title
    reply: dict[str, Any] | None = None  # data to return as it is (ambiguous / not offered)
    error_code: str | None = None
    colour_unverified: bool = False  # a colour was asked, but the product has no colour option


async def _resolve(
    session: AsyncSession,
    product_id: str,
    *,
    variant_id: str | None,
    colour: str | None,
    size_mm: int | None,
) -> Resolution:
    basics = await repo.product_basics(session, product_id)
    if basics is None:
        return Resolution(error_code="not_found")
    rows = await repo.variants_of(session, product_id)
    if not rows:
        return Resolution(error_code="not_found")

    if variant_id:  # an id copied from an earlier tool result wins over colour and size
        chosen = [r for r in rows if r.id == variant_id]
        if not chosen:
            return Resolution(error_code="not_found")
        return Resolution(variant=chosen[0], title=basics.title)

    wanted = colour_of_value(colour) if colour else None
    product_colours = {c for r in rows for c in colours_of(r.options or {})}
    colour_unverified = False
    filter_colour = wanted
    if colour:
        if wanted is None:
            return _not_offered("colour_unknown", sorted(product_colours))
        if product_colours and wanted not in product_colours:
            return _not_offered("colour_not_offered", sorted(product_colours))
        if not product_colours:  # nothing to check the colour against: never claim it
            colour_unverified = True
            filter_colour = None

    matching = [
        r
        for r in rows
        if variant_matches(
            _as_variant_row(r),
            colour=filter_colour,
            size_mm=size_mm,
            max_price=None,
            product_title=basics.title,
        )
    ]
    if not matching:
        reason = "combination_not_offered" if colour and size_mm else "size_not_offered"
        return _not_offered(reason, sorted(product_colours))
    if len(matching) > 1:
        return Resolution(
            title=basics.title,
            reply={
                "match": "ambiguous",
                "product_id": product_id,
                "title": basics.title,
                "variants": [
                    {"variant_id": r.id, "title": r.title}
                    for r in matching[:MAX_AMBIGUOUS_VARIANTS]
                ],
            },
        )
    return Resolution(variant=matching[0], title=basics.title, colour_unverified=colour_unverified)


def _not_offered(reason: str, colours: list[str]) -> Resolution:
    return Resolution(
        reply={
            "match": "none",
            "reason": reason,
            "message": NOT_OFFERED_MESSAGES[reason],
            "colours_offered": colours,
        }
    )


def _live_data(resolution: Resolution, product_id: str, live: Any) -> dict[str, Any]:
    return {
        "match": "one",
        "product_id": product_id,
        "title": resolution.title,
        "variant_id": live.id,
        "variant_title": live.title,
        "colour_unverified": resolution.colour_unverified,
    }


# ---- tool 1: search_products


async def tool_search_products(
    session: AsyncSession,
    query: str,
    *,
    colour: str | None = None,
    size_mm: int | None = None,
    max_price: int | None = None,
) -> ToolResult:
    """Find visible products. No price is returned: ask get_price for it."""

    async def work() -> ToolResult:
        text = (query or "").strip()
        if not text or len(text) > MAX_QUERY_LEN:
            raise ValidationFailed("query must be 1 to 120 characters")
        _check_filters(size_mm, max_price)
        found = await search_products(
            session, text, colour=colour, size_mm=size_mm, max_price=max_price
        )
        return _result(
            TOOL_SEARCH,
            data={
                "kind": found.kind.value,  # one | ambiguous | none
                "colour": found.colour,
                "name_matches": found.name_matches,
                "candidates": [
                    {
                        "product_id": c.product_id,
                        "title": c.title,
                        "url": c.url,
                        "vendor": c.vendor,
                        "score": c.score,
                        "variant_ids": c.variant_ids,
                        "colours": c.colours,
                        "colour_verified": c.colour_verified,
                    }
                    for c in found.candidates
                ],
            },
        )

    return await _guard(TOOL_SEARCH, work())


# ---- tool 2: get_price (live)


async def tool_get_price(
    session: AsyncSession,
    product_id: str,
    *,
    variant_id: str | None = None,
    colour: str | None = None,
    size_mm: int | None = None,
) -> ToolResult:
    """Live price of one variant. If several fit, the result lists them so the bot can ask."""

    async def work() -> ToolResult:
        pid = _product_id(product_id)
        vid = _variant_id(variant_id)
        _check_filters(size_mm)
        res = await _resolve(session, pid, variant_id=vid, colour=colour, size_mm=size_mm)
        if res.error_code:
            return _result(TOOL_PRICE, ok=False, error_code=res.error_code)
        if res.reply is not None:
            return _result(TOOL_PRICE, data=res.reply)
        assert res.variant is not None
        live = await stock_mod.fetch_live_variant(res.variant.id)  # ShopifyError -> _guard
        if live is None or live.product_id != pid:
            return _result(TOOL_PRICE, ok=False, error_code="not_found")
        data = _live_data(res, pid, live)
        data["price"] = price_info(live.price, live.compare_at_price).model_dump(mode="json")
        return _result(TOOL_PRICE, data=data)

    return await _guard(TOOL_PRICE, work())


# ---- tool 3: check_stock (live)


async def tool_check_stock(
    session: AsyncSession,
    product_id: str,
    *,
    variant_id: str | None = None,
    colour: str | None = None,
    size_mm: int | None = None,
) -> ToolResult:
    """Live stock of one variant. If Shopify fails the state is UNKNOWN, never a guess."""

    async def work() -> ToolResult:
        pid = _product_id(product_id)
        vid = _variant_id(variant_id)
        _check_filters(size_mm)
        res = await _resolve(session, pid, variant_id=vid, colour=colour, size_mm=size_mm)
        if res.error_code:
            return _result(TOOL_STOCK, ok=False, error_code=res.error_code)
        if res.reply is not None:
            return _result(TOOL_STOCK, data=res.reply)
        assert res.variant is not None
        try:
            live = await stock_mod.fetch_live_variant(res.variant.id)
        except ShopifyError:
            return _result(
                TOOL_STOCK,
                ok=False,
                error_code="shopify_unavailable",
                data={"stock": stock_mod.unknown_stock().model_dump(mode="json")},
            )
        if live is None or live.product_id != pid:
            return _result(TOOL_STOCK, ok=False, error_code="not_found")
        data = _live_data(res, pid, live)
        info = stock_mod.stock_info(live, show_exact=get_settings().show_exact_stock)
        data["stock"] = info.model_dump(mode="json")
        return _result(TOOL_STOCK, data=data)

    return await _guard(TOOL_STOCK, work())


# ---- tool 4: get_colors_sizes (mirror)


async def tool_get_colors_sizes(session: AsyncSession, product_id: str) -> ToolResult:
    """Colours and case sizes a product is offered in (not stock: see check_stock)."""

    async def work() -> ToolResult:
        pid = _product_id(product_id)
        basics = await repo.product_basics(session, pid)
        if basics is None:
            return _result(TOOL_OPTIONS, ok=False, error_code="not_found")
        rows = await repo.variants_of(session, pid)
        variants: list[dict[str, Any]] = []
        for row in rows:
            options = row.options or {}
            colours = sorted(colours_of(options))
            variants.append(
                {
                    "variant_id": row.id,
                    "title": row.title,
                    "colour": colours[0] if colours else None,
                    "size_mm": variant_size_mm(_as_variant_row(row), basics.title),
                }
            )
        colours_all = sorted({v["colour"] for v in variants if v["colour"]})
        sizes_all = sorted({v["size_mm"] for v in variants if v["size_mm"]})
        return _result(
            TOOL_OPTIONS,
            data={
                "product_id": pid,
                "title": basics.title,
                "colours": colours_all,
                "sizes_mm": sizes_all,
                "colours_listed": bool(colours_all),  # False: the data does not say the colours
                "variants": variants[:MAX_OPTION_VARIANTS],
                "stock_checked": False,
            },
        )

    return await _guard(TOOL_OPTIONS, work())


# ---- tool 5: get_specs (mirror)


async def tool_get_specs(
    session: AsyncSession, product_id: str, spec: str | None = None
) -> ToolResult:
    """Specifications from the merchant's metafields and option sizes. Missing: 'not available'."""

    async def work() -> ToolResult:
        pid = _product_id(product_id)
        if spec is not None and len(spec) > MAX_QUERY_LEN:
            raise ValidationFailed("spec is too long")
        found = await get_specs(session, pid, spec)
        if found is None:
            return _result(TOOL_SPECS, ok=False, error_code="not_found")
        return _result(TOOL_SPECS, data=found.model_dump(mode="json"))

    return await _guard(TOOL_SPECS, work())


# ---- tool 6: get_product_image (mirror)


async def tool_get_product_image(
    session: AsyncSession,
    product_id: str,
    *,
    variant_id: str | None = None,
    colour: str | None = None,
    max_images: int = 1,
) -> ToolResult:
    """Picture URLs exactly as stored. The bot may only send a URL that came from here."""

    async def work() -> ToolResult:
        pid = _product_id(product_id)
        vid = _variant_id(variant_id)
        found = await get_images(session, pid, variant_id=vid, colour=colour, max_images=max_images)
        if found is None:
            return _result(TOOL_IMAGE, ok=False, error_code="not_found")
        return _result(TOOL_IMAGE, data=found.model_dump(mode="json"))

    return await _guard(TOOL_IMAGE, work())


# ---- tool 7: search_policy (knowledge base)


async def tool_search_policy(
    session: AsyncSession,
    query_en: str,
    doc_type: str | None = None,
    *,
    original_query: str | None = None,
    embedder: Embedder | None = None,
) -> ToolResult:
    """Approved passages for a policy or FAQ question, or an abstention.

    `query_en` is the short English search text from the router. The customer's own words
    (`original_query`) are searched too, and are used alone if `query_en` is empty. `doc_type`
    (policy_refund, policy_shipping, policy_privacy, faq, page) narrows the search when the intent
    is clear. Every passage has a `source_id` for BotResponse.source_ids. When `abstain` is true
    the bot must say the answer is not confirmed and offer a human: it must not guess.
    """

    async def work() -> ToolResult:
        text = (query_en or "").strip() or (original_query or "").strip()[:MAX_KB_QUERY_LEN]
        if not text or len(text) > MAX_KB_QUERY_LEN:
            raise ValidationFailed("query_en must be 1 to 200 characters")
        if doc_type is not None and doc_type not in DOC_TYPES:
            raise ValidationFailed("unknown doc_type")
        try:
            found = await retrieve(
                session,
                embedder or get_embedder(),
                query_en=text,
                original=original_query,
                doc_type=doc_type,
            )
        except EmbeddingError:
            return _result(TOOL_POLICY, ok=False, error_code="embedding_unavailable")
        return _result(
            TOOL_POLICY,
            data={
                "abstain": found.abstained,
                "doc_type": doc_type,
                "best_score": None if found.best_score is None else round(found.best_score, 4),
                "passages": [
                    {
                        "source_id": p.source_id,
                        "doc_type": p.doc_type,
                        "heading": p.heading_path,
                        "text": p.text,
                        "url": p.source_url,
                        "score": round(p.score, 4),
                    }
                    for p in found.passages
                ],
            },
        )

    return await _guard(TOOL_POLICY, work())


# ---- registry (Phase 9 / the permission gate will use these names)

ToolFn = Callable[..., Awaitable[ToolResult]]

TOOLS: dict[str, ToolFn] = {
    TOOL_SEARCH: tool_search_products,
    TOOL_PRICE: tool_get_price,
    TOOL_STOCK: tool_check_stock,
    TOOL_OPTIONS: tool_get_colors_sizes,
    TOOL_SPECS: tool_get_specs,
    TOOL_IMAGE: tool_get_product_image,
    TOOL_POLICY: tool_search_policy,
}
