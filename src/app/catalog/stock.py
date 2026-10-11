"""Stock wording and the live variant lookup (Phase 3, step 3; Blueprint 4.10 and 4.6).

The stock STATE is decided in integrations/shopify/mapper.py (classify_stock). This module turns a
state into the sentence the bot may say, and fetches the live variant. The bot never guesses:
if Shopify cannot answer, the state is UNKNOWN.
"""

import time

from pydantic import BaseModel

from app.catalog.normalizer import colour_of_value, colours_of
from app.core.settings import get_settings
from app.domain.catalog import StockState, Variant
from app.integrations.shopify import gateway

STOCK_MESSAGES: dict[StockState, str] = {
    StockState.IN_STOCK: "Available.",
    StockState.LOW_STOCK: "Limited stock.",
    StockState.OUT_OF_STOCK: "Currently out of stock.",
    StockState.BACKORDER: (
        "Can be ordered, but delivery may take longer; please confirm with the team."
    ),
    StockState.UNKNOWN: "I cannot check stock right now.",
    StockState.COLOUR_NOT_OFFERED: "This model does not come in that colour.",
}


class StockInfo(BaseModel):
    state: StockState
    message: str  # the only stock sentence the bot may base its reply on
    quantity: int | None = None  # filled only when the client allows exact counts (A7)


def stock_info(variant: Variant, *, show_exact: bool) -> StockInfo:
    """Customer-safe stock facts for one variant.

    An exact number is given only if SHOW_EXACT_STOCK is on, the variant is really in stock and
    the count is above zero. 'Not tracked' variants have no usable count, so none is shown.
    """
    quantity = variant.inventory_quantity
    exact = (
        show_exact
        and variant.stock in (StockState.IN_STOCK, StockState.LOW_STOCK)
        and quantity is not None
        and quantity > 0
    )
    return StockInfo(
        state=variant.stock,
        message=STOCK_MESSAGES[variant.stock],
        quantity=quantity if exact else None,
    )


def unknown_stock() -> StockInfo:
    """Used when Shopify fails or times out. Never guess."""
    return StockInfo(state=StockState.UNKNOWN, message=STOCK_MESSAGES[StockState.UNKNOWN])


def colour_not_offered() -> StockInfo:
    """No variant has that colour at all. This is NOT the same as out of stock."""
    return StockInfo(
        state=StockState.COLOUR_NOT_OFFERED,
        message=STOCK_MESSAGES[StockState.COLOUR_NOT_OFFERED],
    )


def variants_with_colour(variants: list[Variant], colour: str) -> list[Variant]:
    """Variants of one product that have this colour. 'kala' finds the Black variant."""
    wanted = colour_of_value(colour)
    if not wanted:
        return []
    return [v for v in variants if wanted in colours_of(v.options)]


# ---- live lookup with an optional very short cache (LIVE_STOCK_CACHE_SECONDS)

_MAX_CACHED = 500
_cache: dict[str, tuple[float, Variant]] = {}
_now = time.monotonic  # a module attribute so tests can replace the clock


def clear_live_cache() -> None:
    _cache.clear()


async def fetch_live_variant(variant_id: str, *, ttl_seconds: int | None = None) -> Variant | None:
    """Live price and stock of one variant (a fresh Shopify call unless the cache is on).

    Default is no cache (LIVE_STOCK_CACHE_SECONDS=0). Shopify errors are NOT caught here:
    the tool layer turns them into stock state UNKNOWN. A missing or hidden variant is None.
    """
    ttl = get_settings().live_stock_cache_seconds if ttl_seconds is None else ttl_seconds
    if ttl > 0:
        hit = _cache.get(variant_id)
        if hit and _now() - hit[0] < ttl:
            return hit[1]
    variant = await gateway.get_variant(variant_id)
    if variant is not None and ttl > 0:
        if len(_cache) >= _MAX_CACHED:
            _cache.clear()
        _cache[variant_id] = (_now(), variant)
    return variant
