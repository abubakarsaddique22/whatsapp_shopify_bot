"""Shopify webhook delivery: read the headers and body into one validated object (Phase 10, step 2).

Pure functions, no database and no network. The endpoint (api/shopify_webhook.py) checks the
signature first, then calls parse_delivery(). A webhook only SAYS that something changed; the
handlers (step 4) refetch the object by id, so we keep just a few small fields of the body.
"""

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel

# Topics the bot reacts to (Blueprint 4.7.2). Everything else is answered 200 and not stored.
SUPPORTED_TOPICS = frozenset(
    {
        "products/create",
        "products/update",
        "products/delete",
        "inventory_levels/update",
    }
)

# Only these body fields are kept: ids, the update time and the stock numbers. Titles,
# descriptions, images and variants are NOT kept, because the handler refetches them anyway.
KEPT_PAYLOAD_KEYS = (
    "id",
    "admin_graphql_api_id",
    "updated_at",
    "inventory_item_id",
    "location_id",
    "available",
)


class ShopifyDelivery(BaseModel):
    """One webhook delivery, ready to be stored as a sync_events row."""

    webhook_id: str  # the idempotency key (X-Shopify-Event-Id, see parse_delivery)
    topic: str
    shop_domain: str
    resource_id: str | None  # Shopify global id of the product or inventory item
    triggered_at: datetime | None
    payload: dict[str, Any]


def resource_gid(topic: str, payload: Mapping[str, Any]) -> str | None:
    """Shopify global id of the object an event is about, or None if the body does not say.

    products/*         -> gid://shopify/Product/<id>        (products/delete only has the number)
    inventory_levels/* -> gid://shopify/InventoryItem/<inventory_item_id>
    """
    if topic.startswith("products/"):
        gid = payload.get("admin_graphql_api_id")
        if isinstance(gid, str) and gid:
            return gid
        number = payload.get("id")
        return f"gid://shopify/Product/{number}" if number is not None else None
    if topic.startswith("inventory_levels/"):
        item = payload.get("inventory_item_id")
        return f"gid://shopify/InventoryItem/{item}" if item is not None else None
    gid = payload.get("admin_graphql_api_id")
    return gid if isinstance(gid, str) and gid else None


def parse_triggered_at(value: str | None) -> datetime | None:
    """X-Shopify-Triggered-At looks like 2023-03-29T18:00:27.877041743Z (nanoseconds).
    Python reads at most 6 fraction digits, so the extra digits are cut off first."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value.strip()))
    except ValueError:
        return None


def parse_delivery(headers: Mapping[str, str], raw: bytes) -> ShopifyDelivery | None:
    """Turn the headers and the raw body into a ShopifyDelivery.

    Returns None when the delivery is unusable (no id, no topic, no shop, body not a JSON
    object). The caller still answers 200, so Shopify does not retry a delivery we can never use.

    Duplicate detection: Shopify's own advice is the X-Shopify-Event-Id header (the same value on
    a repeated delivery). X-Shopify-Webhook-Id is only the fallback if that header is missing.
    """
    h = {key.lower(): value for key, value in headers.items()}
    webhook_id = (h.get("x-shopify-event-id") or h.get("x-shopify-webhook-id") or "").strip()
    topic = (h.get("x-shopify-topic") or "").strip()
    shop_domain = (h.get("x-shopify-shop-domain") or "").strip()
    if not webhook_id or not topic or not shop_domain:
        return None

    try:
        body = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None

    return ShopifyDelivery(
        webhook_id=webhook_id[:100],
        topic=topic[:64],
        shop_domain=shop_domain[:200],
        resource_id=resource_gid(topic, body),
        triggered_at=parse_triggered_at(h.get("x-shopify-triggered-at")),
        payload={key: body[key] for key in KEPT_PAYLOAD_KEYS if key in body},
    )
