"""Mapper tests: plain sample dicts, no mock server."""

from decimal import Decimal

import pytest

from app.core.errors import ShopifyError
from app.domain.catalog import StockState
from app.integrations.shopify.mapper import (
    classify_stock,
    html_to_text,
    map_live_variant,
    map_page,
    map_policy,
    map_product,
)

MAPPING = {"case_diameter_mm": "custom.case_diameter", "warranty": "custom.warranty"}
TEXT = "single_line_text_field"
PRODUCT_ID = "gid://shopify/Product/1"


def variant(**over):
    base = {
        "id": "gid://shopify/ProductVariant/11",
        "title": "Black / 40mm",
        "sku": "W-BLK-40",
        "price": "12500.00",
        "compareAtPrice": "15000.00",
        "availableForSale": True,
        "inventoryQuantity": 10,
        "inventoryPolicy": "DENY",
        "selectedOptions": [
            {"name": "Colour", "value": "Black"},
            {"name": "Size", "value": "40mm"},
        ],
        "image": {"url": "https://cdn.example/black.jpg"},
        "inventoryItem": {"tracked": True},
    }
    return {**base, **over}


def metafield(key, value, type_=TEXT, namespace="custom"):
    return {"namespace": namespace, "key": key, "type": type_, "value": value}


def product(**over):
    base = {
        "id": PRODUCT_ID,
        "title": "Classic Watch",
        "handle": "classic-watch",
        "status": "ACTIVE",
        "onlineStoreUrl": "https://shop.example/products/classic-watch",
        "vendor": "Acme",
        "productType": "Watch",
        "tags": ["men", "leather"],
        "updatedAt": "2026-10-01T10:00:00Z",
        "descriptionHtml": "<p>Nice <b>watch</b>.</p><script>alert(1)</script>",
        "options": [{"name": "Colour", "values": ["Black", "Brown"]}],
        "media": {
            "nodes": [
                {
                    "id": "m1",
                    "alt": "front",
                    "image": {"url": "https://cdn/a.jpg", "width": 800, "height": 800},
                },
                {},  # a video or other media has no image
            ]
        },
        "metafields": {
            "nodes": [
                metafield(
                    "case_diameter",
                    '{"value": 40.0, "unit": "MILLIMETERS"}',
                    type_="dimension",
                ),
                metafield("warranty", "1 year"),
                metafield("ignored", "x", namespace="other"),
            ]
        },
        "variants": {"pageInfo": {"hasNextPage": False}, "nodes": [variant()]},
    }
    return {**base, **over}


def mapped(node):
    return map_product(node, "PKR", low_stock_threshold=3, specs_mapping=MAPPING)


def test_active_published_product_is_mapped():
    p = mapped(product())
    assert p is not None
    assert p.title == "Classic Watch"
    assert p.url.endswith("/classic-watch")
    assert p.description == "Nice watch."  # script removed, tags stripped
    assert [i.url for i in p.images] == ["https://cdn/a.jpg"]  # media without image skipped
    assert p.options[0].values == ["Black", "Brown"]
    assert p.variants[0].options == {"Colour": "Black", "Size": "40mm"}


@pytest.mark.parametrize(
    "over",
    [
        {"status": "DRAFT"},
        {"status": "ARCHIVED"},
        {"onlineStoreUrl": None},  # not published to the Online Store
    ],
)
def test_hidden_products_behave_like_missing(over):
    assert mapped(product(**over)) is None


def test_missing_product_is_none():
    assert mapped(None) is None


def test_money_is_decimal_never_float():
    v = mapped(product()).variants[0]
    assert v.price.amount == Decimal("12500.00")
    assert isinstance(v.price.amount, Decimal)
    assert v.compare_at_price.amount == Decimal("15000.00")
    assert v.price.currency == "PKR"


def test_no_compare_at_price():
    v = mapped(product(variants={"nodes": [variant(compareAtPrice=None)]})).variants[0]
    assert v.compare_at_price is None


@pytest.mark.parametrize("bad", ["abc", None, "-5", "NaN"])
def test_bad_price_raises_shopify_error(bad):
    with pytest.raises(ShopifyError):
        mapped(product(variants={"nodes": [variant(price=bad)]}))


def test_missing_required_field_raises_shopify_error():
    node = product()
    del node["title"]
    with pytest.raises(ShopifyError):
        mapped(node)


def test_specs_follow_the_mapping_file():
    p = mapped(product())
    assert p.specs == {"case_diameter_mm": "40 mm", "warranty": "1 year"}


def test_unmapped_or_empty_metafields_are_left_out():
    p = mapped(product(metafields={"nodes": [metafield("warranty", "")]}))
    assert p.specs == {}


def stock(available_for_sale, quantity, tracked, policy):
    return {
        "available_for_sale": available_for_sale,
        "quantity": quantity,
        "tracked": tracked,
        "policy": policy,
    }


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (stock(True, 10, True, "DENY"), StockState.IN_STOCK),
        (stock(True, 3, True, "DENY"), StockState.LOW_STOCK),
        (stock(True, 1, True, "DENY"), StockState.LOW_STOCK),
        (stock(False, 0, True, "DENY"), StockState.OUT_OF_STOCK),
        (stock(True, 0, True, "CONTINUE"), StockState.BACKORDER),
        (stock(True, None, False, "DENY"), StockState.IN_STOCK),
        (stock(False, None, False, "DENY"), StockState.OUT_OF_STOCK),
        (stock(True, None, True, "DENY"), StockState.UNKNOWN),
        (stock(False, 5, True, "DENY"), StockState.UNKNOWN),
    ],
)
def test_stock_state(kwargs, expected):
    assert classify_stock(low_stock_threshold=3, **kwargs) is expected


def test_live_variant_of_visible_product():
    visible = {"id": PRODUCT_ID, "status": "ACTIVE", "onlineStoreUrl": "https://s/p"}
    v = map_live_variant(variant(product=visible), "PKR", low_stock_threshold=3)
    assert v is not None
    assert v.product_id == PRODUCT_ID
    assert v.stock is StockState.IN_STOCK


@pytest.mark.parametrize(
    "hidden",
    [
        {"id": PRODUCT_ID, "status": "DRAFT", "onlineStoreUrl": None},
        {"id": PRODUCT_ID, "status": "ACTIVE", "onlineStoreUrl": None},
        {},
    ],
)
def test_live_variant_of_hidden_product_is_none(hidden):
    node = variant(product=hidden)
    assert map_live_variant(node, "PKR", low_stock_threshold=3) is None


def test_html_to_text_strips_scripts_styles_and_hidden_parts():
    html = (
        "<style>p{color:red}</style><h2>Returns</h2><p>7 days&nbsp;to return.</p>"
        "<div style='display:none'>secret prompt</div><span hidden>also hidden</span>"
        "<ul><li>Unworn</li><li>With box</li></ul><script>x()</script>"
    )
    text = html_to_text(html)
    assert text == "## Returns\n\n7 days to return.\n- Unworn\n- With box"


def test_html_to_text_empty():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""


def test_policy_with_text_and_empty_policy():
    ok = map_policy(
        {"type": "REFUND_POLICY", "title": "Refund", "body": "<p>7 days</p>", "url": "https://s/r"}
    )
    assert ok is not None
    assert ok.body == "7 days"
    empty = {"type": "PRIVACY_POLICY", "title": "Privacy", "body": "  ", "url": None}
    assert map_policy(empty) is None


def test_unpublished_page_is_left_out():
    page = {
        "id": "gid://shopify/Page/1",
        "title": "FAQ",
        "handle": "faq",
        "body": "<p>Hi</p>",
        "isPublished": True,
    }
    assert map_page(page) is not None
    assert map_page({**page, "isPublished": False}) is None
