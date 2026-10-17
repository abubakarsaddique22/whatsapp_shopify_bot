"""Read-only gateway to Shopify: products, variants, policies and pages.

Every function returns our catalogue models (domain/catalog.py). Hidden products (draft, archived,
unpublished) are never returned: `list_products` leaves them out and `get_product` / `get_variant`
return None, exactly as if they did not exist.
"""

import re
from datetime import UTC, datetime

from app.core.errors import ShopifyError, ValidationFailed
from app.core.settings import get_settings
from app.domain.catalog import Page, Policy, Product, Variant
from app.integrations.shopify import queries as q
from app.integrations.shopify.client import graphql
from app.integrations.shopify.mapper import (
    load_specs_mapping,
    map_live_variant,
    map_page,
    map_policy,
    map_product,
)

MAX_PAGES = 100  # safety stop so a pagination bug can never loop forever


def _gid(value: str, kind: str) -> str:
    """Accept only Shopify global ids like gid://shopify/Product/123 (ids may come from an LLM)."""
    if not re.fullmatch(rf"gid://shopify/{kind}/\d+", value or ""):
        raise ValidationFailed(f"Not a valid Shopify {kind} id")
    return value


def _currency(data: dict) -> str:
    try:
        return str(data["shop"]["currencyCode"])
    except (KeyError, TypeError) as exc:
        raise ShopifyError("Shopify did not return the shop currency") from exc


async def _all_nodes(query: str, field: str, variables: dict) -> tuple[dict, list[dict]]:
    """Follow the cursor until the last page. Returns (last response data, all nodes)."""
    after: str | None = None
    nodes: list[dict] = []
    for _ in range(MAX_PAGES):
        data = await graphql(query, {**variables, "after": after})
        connection = data.get(field) or {}
        nodes.extend(connection.get("nodes") or [])
        page_info = connection.get("pageInfo") or {}
        after = page_info.get("endCursor")
        if not page_info.get("hasNextPage") or not after:
            return data, nodes
    raise ShopifyError(f"Stopped after {MAX_PAGES} pages of '{field}'")


async def list_products() -> list[Product]:
    """All customer-visible products with their variants."""
    data, nodes = await _all_nodes(
        q.PRODUCTS_PAGE, "products", {"first": q.PRODUCTS_PAGE_SIZE, "q": "status:active"}
    )
    currency = _currency(data)
    settings = get_settings()
    mapping = load_specs_mapping()
    products: dict[str, Product] = {}  # by id: a product edited mid-pagination can appear twice
    for node in nodes:
        product = map_product(
            node,
            currency,
            low_stock_threshold=settings.low_stock_threshold,
            specs_mapping=mapping,
        )
        if product:
            products[product.id] = product
    return list(products.values())


def shopify_timestamp(moment: datetime) -> str:
    """2026-10-17T12:30:05Z: the form Shopify's search accepts for a date (`moment` is aware)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def list_products_changed_since(since: datetime) -> dict[str, Product | None]:
    """Every product edited after `since`, whatever its status, as {id: Product or None}.

    None means the product is not customer-visible now (draft, archived, unpublished): the
    caller must hide it in the mirror. A product DELETED in Shopify is not returned at all; the
    nightly full pass finds those. Read-only query, same page size and cost as list_products.
    """
    search = f"updated_at:>'{shopify_timestamp(since)}'"
    data, nodes = await _all_nodes(
        q.PRODUCTS_PAGE, "products", {"first": q.PRODUCTS_PAGE_SIZE, "q": search}
    )
    currency = _currency(data)
    settings = get_settings()
    mapping = load_specs_mapping()
    changed: dict[str, Product | None] = {}
    for node in nodes:
        product_id = node.get("id")
        if not product_id:
            continue
        changed[product_id] = map_product(
            node,
            currency,
            low_stock_threshold=settings.low_stock_threshold,
            specs_mapping=mapping,
        )
    return changed


async def get_product(product_id: str) -> Product | None:
    """One product by Shopify id, or None if it does not exist or is not customer-visible."""
    data = await graphql(q.PRODUCT_BY_ID, {"id": _gid(product_id, "Product")})
    return map_product(
        data.get("product"),
        _currency(data),
        low_stock_threshold=get_settings().low_stock_threshold,
        specs_mapping=load_specs_mapping(),
    )


async def get_variant(variant_id: str) -> Variant | None:
    """Live price and stock of one variant (always a fresh call, never cached here)."""
    data = await graphql(q.VARIANT_BY_ID, {"id": _gid(variant_id, "ProductVariant")})
    return map_live_variant(
        data.get("productVariant"),
        _currency(data),
        low_stock_threshold=get_settings().low_stock_threshold,
    )


async def product_id_of_inventory_item(item_id: str) -> str | None:
    """Shopify id of the product an inventory item belongs to, or None if it has no product.

    The inventory_levels/update webhook only carries the inventory item id, so the sync handler
    needs this to know which product to refetch."""
    data = await graphql(q.PRODUCT_OF_INVENTORY_ITEM, {"id": _gid(item_id, "InventoryItem")})
    item = data.get("inventoryItem") or {}
    product = (item.get("variant") or {}).get("product") or {}
    product_id = product.get("id")
    return str(product_id) if product_id else None


async def list_policies() -> list[Policy]:
    """Shop policies (refund, shipping, privacy, terms) that have text. Missing ones are absent."""
    data = await graphql(q.POLICIES)
    nodes = ((data.get("shop") or {}).get("shopPolicies")) or []
    return [policy for node in nodes if (policy := map_policy(node))]


async def list_pages() -> list[Page]:
    """Published online-store pages (FAQ, warranty, size guide) that have text."""
    _, nodes = await _all_nodes(q.PAGES, "pages", {"first": q.PAGES_PAGE_SIZE})
    return [page for node in nodes if (page := map_page(node))]
