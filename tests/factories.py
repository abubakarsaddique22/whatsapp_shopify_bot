"""Small builders for unit tests of pure functions (prices, stock wording).

These are NOT store data: search, tools and the CLI are tested against the real dev store.
The sync handler tests (tests/integration/test_sync_handlers.py) use make_product only to control
what a pretend Shopify answers (deleted, down, price changed); every row they write is rolled
back at the end of the test, so nothing stays in the dev database.
"""

from decimal import Decimal

from app.domain.catalog import Money, Product, StockState, Variant


def make_variant(**overrides: object) -> Variant:
    base: dict[str, object] = {
        "id": "gid://shopify/ProductVariant/1",
        "product_id": "gid://shopify/Product/1",
        "title": "Black",
        "price": Money(amount=Decimal("12500"), currency="PKR"),
        "options": {"Colour": "Black"},
        "available_for_sale": True,
        "inventory_quantity": 10,
        "stock": StockState.IN_STOCK,
    }
    return Variant.model_validate({**base, **overrides})


def make_product(product_id: str = "gid://shopify/Product/9990001", **overrides: object) -> Product:
    """A visible product with one variant. The variant id is the product number followed by 1
    (Product/9990001 -> ProductVariant/99900011)."""
    number = product_id.rsplit("/", 1)[1]
    variant = make_variant(id=f"gid://shopify/ProductVariant/{number}1", product_id=product_id)
    base: dict[str, object] = {
        "id": product_id,
        "title": "Sync Test Watch",
        "handle": "sync-test-watch",
        "url": "https://example.com/products/sync-test-watch",
        "variants": [variant],
    }
    return Product.model_validate({**base, **overrides})
