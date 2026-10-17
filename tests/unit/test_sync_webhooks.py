"""Unit tests for sync/webhooks.py (Phase 10, step 2). No database, no network."""

import json
from datetime import UTC

from app.sync.webhooks import parse_delivery, parse_triggered_at, resource_gid

HEADERS = {
    "X-Shopify-Topic": "products/update",
    "X-Shopify-Shop-Domain": "test-shop.myshopify.com",
    "X-Shopify-Event-Id": "event-111",
    "X-Shopify-Webhook-Id": "webhook-999",
    "X-Shopify-Triggered-At": "2026-10-16T18:00:27.877041743Z",
}


def body(**fields: object) -> bytes:
    return json.dumps(fields).encode()


# ---- resource_gid


def test_product_event_uses_the_graphql_id_when_present() -> None:
    gid = "gid://shopify/Product/42"
    assert resource_gid("products/update", {"id": 42, "admin_graphql_api_id": gid}) == gid


def test_product_delete_builds_the_gid_from_the_number() -> None:
    assert resource_gid("products/delete", {"id": 42}) == "gid://shopify/Product/42"


def test_inventory_event_points_to_the_inventory_item() -> None:
    payload = {"inventory_item_id": 77, "location_id": 5, "available": 3}
    assert resource_gid("inventory_levels/update", payload) == "gid://shopify/InventoryItem/77"


def test_event_without_any_id_has_no_resource() -> None:
    assert resource_gid("products/update", {}) is None
    assert resource_gid("inventory_levels/update", {}) is None


# ---- parse_triggered_at


def test_triggered_at_with_nanoseconds_is_read() -> None:
    parsed = parse_triggered_at("2026-10-16T18:00:27.877041743Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.astimezone(UTC).hour == 18


def test_triggered_at_missing_or_broken_is_none() -> None:
    assert parse_triggered_at(None) is None
    assert parse_triggered_at("") is None
    assert parse_triggered_at("not a date") is None


# ---- parse_delivery


def test_delivery_is_read_from_headers_and_body() -> None:
    raw = body(id=42, admin_graphql_api_id="gid://shopify/Product/42", title="Watch")
    delivery = parse_delivery(HEADERS, raw)
    assert delivery is not None
    assert delivery.webhook_id == "event-111"  # the event id wins over the webhook id
    assert delivery.topic == "products/update"
    assert delivery.shop_domain == "test-shop.myshopify.com"
    assert delivery.resource_id == "gid://shopify/Product/42"
    assert delivery.triggered_at is not None


def test_only_small_fields_of_the_body_are_kept() -> None:
    raw = body(id=42, title="Watch", body_html="<p>long text</p>", variants=[{"price": "1"}])
    delivery = parse_delivery(HEADERS, raw)
    assert delivery is not None
    assert delivery.payload == {"id": 42}


def test_webhook_id_is_used_when_the_event_id_is_missing() -> None:
    headers = {k: v for k, v in HEADERS.items() if k != "X-Shopify-Event-Id"}
    delivery = parse_delivery(headers, body(id=1))
    assert delivery is not None
    assert delivery.webhook_id == "webhook-999"


def test_header_names_are_case_insensitive() -> None:
    lowered = {k.lower(): v for k, v in HEADERS.items()}
    assert parse_delivery(lowered, body(id=1)) is not None


def test_delivery_without_any_id_is_unusable() -> None:
    headers = {
        k: v for k, v in HEADERS.items() if k not in ("X-Shopify-Event-Id", "X-Shopify-Webhook-Id")
    }
    assert parse_delivery(headers, body(id=1)) is None


def test_delivery_without_topic_or_shop_is_unusable() -> None:
    no_topic = {k: v for k, v in HEADERS.items() if k != "X-Shopify-Topic"}
    no_shop = {k: v for k, v in HEADERS.items() if k != "X-Shopify-Shop-Domain"}
    assert parse_delivery(no_topic, body(id=1)) is None
    assert parse_delivery(no_shop, body(id=1)) is None


def test_body_that_is_not_a_json_object_is_unusable() -> None:
    assert parse_delivery(HEADERS, b"this is not json") is None
    assert parse_delivery(HEADERS, b"[1, 2, 3]") is None
