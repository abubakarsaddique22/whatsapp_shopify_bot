"""Import the real store into the Postgres catalogue mirror (Phase 3, step 4).

    uv run python -m app.cli import-catalog

Safe to run again and again: a product whose content did not change is skipped, a product that
Shopify no longer shows is hidden (is_visible = false), and one failure rolls the whole run back.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repositories as repo
from app.domain.catalog import Product

# Stock changes all day and the mirror does not store it, so it must not count as a product change.
_VOLATILE = {"variants": {"__all__": {"inventory_quantity", "stock"}}}


@dataclass
class ImportReport:
    total: int = 0  # visible products Shopify returned
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    hidden: int = 0  # in the mirror, but no longer visible in Shopify
    skipped_hiding: bool = False  # Shopify returned nothing, so nothing was hidden


def compute_hash(product: Product) -> str:
    """Same product data gives the same hash; any change to what the mirror stores changes it."""
    data = product.model_dump(mode="json", exclude=_VOLATILE)
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def product_row(product: Product, content_hash: str) -> dict[str, Any]:
    return {
        "id": product.id,
        "handle": product.handle,
        "title": product.title,
        "url": product.url,
        "vendor": product.vendor,
        "product_type": product.product_type,
        "tags": list(product.tags),
        "options": [option.model_dump() for option in product.options],
        "specs": dict(product.specs),
        "description_text": product.description,
        "is_visible": True,
        "content_hash": content_hash,
        "updated_at_shopify": product.updated_at,
    }


def variant_rows(product: Product) -> list[dict[str, Any]]:
    return [
        {
            "id": v.id,
            "product_id": product.id,
            "title": v.title,
            "sku": v.sku,
            "options": dict(v.options),
            "price_amount": v.price.amount,
            "currency": v.price.currency,
            "available_for_sale": v.available_for_sale,
            "image_url": v.image_url,
        }
        for v in product.variants
    ]


def image_rows(product: Product) -> list[dict[str, Any]]:
    return [
        {
            "product_id": product.id,
            "position": position,
            "url": image.url,
            "alt": image.alt,
            "width": image.width,
            "height": image.height,
        }
        for position, image in enumerate(product.images)
    ]


async def sync_mirror(session: AsyncSession, products: list[Product]) -> ImportReport:
    """Bring the mirror in line with `products` (the visible products of the store).

    The caller commits. Nothing is hidden when `products` is empty: an empty answer from Shopify
    is far more likely a problem than a store with no products, and it must not wipe the mirror.
    """
    report = ImportReport(total=len(products))
    state = await repo.mirror_state(session)

    for product in products:
        content_hash = compute_hash(product)
        known = state.get(product.id)
        if known is not None and known == (content_hash, True):
            report.unchanged += 1
            continue
        await repo.upsert_product(
            session,
            product_row(product, content_hash),
            variant_rows(product),
            image_rows(product),
        )
        if known is None:
            report.added += 1
        else:
            report.updated += 1

    if products:
        report.hidden = await repo.hide_missing(session, [p.id for p in products])
    else:
        report.skipped_hiding = True
    return report
