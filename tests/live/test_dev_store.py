"""Tests against the real Shopify dev store (read-only). Skipped unless you ask for them:

    $env:RUN_LIVE="1"; uv run pytest tests\\live -m live -v
"""

import os
from decimal import Decimal

import pytest

from app.core.errors import ValidationFailed
from app.domain.catalog import Product, StockState
from app.integrations.shopify import gateway

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("RUN_LIVE"), reason="set RUN_LIVE=1 to use the real store"),
]


@pytest.fixture
async def products() -> list[Product]:
    return await gateway.list_products()


async def test_products_are_visible_and_priced_in_decimal(products):
    assert products, "the dev store has no active, published products"
    for p in products:
        assert p.url.startswith("https://")
        assert p.variants, f"{p.title} has no variants"
        for v in p.variants:
            assert isinstance(v.price.amount, Decimal)
            assert v.price.currency == products[0].variants[0].price.currency
            assert v.stock in set(StockState)


async def test_product_ids_are_unique(products):
    ids = [p.id for p in products]
    assert len(ids) == len(set(ids))


async def test_get_product_matches_the_list(products):
    first = products[0]
    again = await gateway.get_product(first.id)
    assert again is not None
    assert again.title == first.title
    assert [v.id for v in again.variants] == [v.id for v in first.variants]


async def test_live_variant_has_current_price(products):
    listed = products[0].variants[0]
    live = await gateway.get_variant(listed.id)
    assert live is not None
    assert live.price.amount == listed.price.amount
    assert live.product_id == products[0].id


async def test_unknown_ids_behave_like_missing():
    assert await gateway.get_product("gid://shopify/Product/1") is None
    assert await gateway.get_variant("gid://shopify/ProductVariant/1") is None


async def test_bad_id_is_rejected_before_any_request():
    with pytest.raises(ValidationFailed):
        await gateway.get_product("not-an-id")


async def test_policies_and_pages_have_text():
    for policy in await gateway.list_policies():
        assert policy.body.strip()
    for page in await gateway.list_pages():
        assert page.body.strip()
