"""Small builders for unit tests of pure functions (prices, stock wording).

These are NOT store data: search, tools and the CLI are tested against the real dev store.
"""

from decimal import Decimal

from app.domain.catalog import Money, StockState, Variant


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
