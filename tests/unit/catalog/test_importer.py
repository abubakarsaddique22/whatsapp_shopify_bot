"""Mirror import helpers (Phase 3, step 4): hashing and row mapping. No database needed."""

from decimal import Decimal

from app.catalog.importer import compute_hash, image_rows, product_row, variant_rows
from app.domain.catalog import Image, Money, Product, ProductOption, StockState, Variant


def make_product(**changes) -> Product:
    variant = Variant(
        id="gid://shopify/ProductVariant/1",
        product_id="gid://shopify/Product/1",
        title="Black / 40mm",
        sku="GA-2100-1A",
        price=Money(amount=Decimal("12500.00"), currency="PKR"),
        options={"Colour": "Black", "Case size": "40mm"},
        available_for_sale=True,
        inventory_quantity=5,
        stock=StockState.IN_STOCK,
        image_url="https://cdn.example/black.jpg",
    )
    data = {
        "id": "gid://shopify/Product/1",
        "title": "Casio G-Shock GA-2100",
        "handle": "ga-2100",
        "url": "https://shop.example/products/ga-2100",
        "vendor": "Casio",
        "tags": ["watch", "g-shock"],
        "description": "Carbon core guard.",
        "options": [ProductOption(name="Colour", values=["Black"])],
        "specs": {"water_resistance": "200m"},
        "images": [Image(url="https://cdn.example/1.jpg"), Image(url="https://cdn.example/2.jpg")],
        "variants": [variant],
    }
    return Product(**{**data, **changes})


def test_hash_is_stable():
    assert compute_hash(make_product()) == compute_hash(make_product())


def test_hash_changes_when_title_changes():
    assert compute_hash(make_product()) != compute_hash(make_product(title="Casio GA-2100 Black"))


def test_hash_changes_when_price_changes():
    product = make_product()
    cheaper = product.variants[0].model_copy(
        update={"price": Money(amount=Decimal("11000.00"), currency="PKR")}
    )
    assert compute_hash(product) != compute_hash(make_product(variants=[cheaper]))


def test_hash_ignores_stock_changes():
    product = make_product()
    sold_out = product.variants[0].model_copy(
        update={"inventory_quantity": 0, "stock": StockState.OUT_OF_STOCK}
    )
    assert compute_hash(product) == compute_hash(make_product(variants=[sold_out]))


def test_product_row_maps_fields():
    row = product_row(make_product(), "abc")
    assert row["id"] == "gid://shopify/Product/1"
    assert row["description_text"] == "Carbon core guard."
    assert row["options"] == [{"name": "Colour", "values": ["Black"]}]
    assert row["is_visible"] is True
    assert row["content_hash"] == "abc"


def test_variant_rows_keep_decimal_price():
    rows = variant_rows(make_product())
    assert len(rows) == 1
    assert rows[0]["price_amount"] == Decimal("12500.00")
    assert rows[0]["currency"] == "PKR"
    assert rows[0]["options"] == {"Colour": "Black", "Case size": "40mm"}


def test_image_rows_are_numbered_from_zero():
    rows = image_rows(make_product())
    assert [r["position"] for r in rows] == [0, 1]
    assert rows[0]["product_id"] == "gid://shopify/Product/1"
