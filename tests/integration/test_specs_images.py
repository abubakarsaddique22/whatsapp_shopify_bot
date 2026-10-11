"""Tool-level T04 (specs and sizes) and picture choice on the real Postgres.

Same method as test_search.py: made-up 'Vantor' products inside a transaction that is rolled back,
so the real mirror is never touched.
"""

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.catalog.images import NO_IMAGE_MESSAGE, get_images
from app.catalog.importer import compute_hash, image_rows, product_row, variant_rows
from app.catalog.specs import NOT_AVAILABLE, get_specs
from app.core.settings import get_settings
from app.db import repositories as repo
from app.domain.catalog import Image, Money, Product, StockState, Variant

REEF_ID = "gid://shopify/Product/9101"
BARE_ID = "gid://shopify/Product/9102"
HIDDEN_ID = "gid://shopify/Product/9103"
BLUE_VARIANT = "gid://shopify/ProductVariant/910102"


def variant(product_no: int, n: int, colour: str, size: str, image: str | None) -> Variant:
    return Variant(
        id=f"gid://shopify/ProductVariant/{product_no}0{n}",
        product_id=f"gid://shopify/Product/{product_no}",
        title=f"{colour} / {size}",
        price=Money(amount=Decimal("9000"), currency="PKR"),
        options={"Colour": colour, "Case size": size},
        available_for_sale=True,
        inventory_quantity=3,
        stock=StockState.IN_STOCK,
        image_url=image,
    )


REEF = Product(
    id=REEF_ID,
    title="Vantor Reef",
    handle="vantor-reef",
    url="https://shop.example/products/vantor-reef",
    specs={"water_resistance": "200 m", "warranty": "2 years"},
    images=[
        Image(url="https://cdn.example/reef-1.jpg?v=1", alt="front"),
        Image(url="https://cdn.example/reef-blue.jpg?v=5"),
        Image(url="https://cdn.example/reef-3.jpg"),
        Image(url="https://cdn.example/reef-4.jpg"),
    ],
    variants=[
        variant(9101, 1, "Black", "40mm", None),  # black has no picture of its own
        variant(9101, 2, "Blue", "44mm", "https://cdn.example/reef-blue.jpg?v=7"),
    ],
)

BARE = Product(  # no specs, no pictures, no colour or size options
    id=BARE_ID,
    title="Vantor Bare",
    handle="vantor-bare",
    url="https://shop.example/products/vantor-bare",
    variants=[
        Variant(
            id="gid://shopify/ProductVariant/910201",
            product_id=BARE_ID,
            title="Default Title",
            price=Money(amount=Decimal("5000"), currency="PKR"),
            options={"Title": "Default Title"},
            available_for_sale=True,
            inventory_quantity=2,
            stock=StockState.IN_STOCK,
        )
    ],
)

HIDDEN = REEF.model_copy(update={"id": HIDDEN_ID, "handle": "vantor-hidden", "variants": []})


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            for product, visible in ((REEF, True), (BARE, True), (HIDDEN, False)):
                row = product_row(product, compute_hash(product)) | {"is_visible": visible}
                await repo.upsert_product(db, row, variant_rows(product), image_rows(product))
            yield db
            await db.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


# ---- T04: specs and sizes


async def test_t04_asked_spec_comes_from_the_metafield(session):
    result = await get_specs(session, REEF_ID, "waterproof?")
    assert result is not None
    assert result.available
    assert [(s.name, s.value) for s in result.specs] == [("water_resistance", "200 m")]


async def test_t04_case_size_comes_from_the_variant_options(session):
    result = await get_specs(session, REEF_ID, "case size")
    assert result is not None
    assert result.specs[0].value == "40 mm, 44 mm"
    assert result.specs[0].source == "option"


async def test_t04_missing_value_is_not_available(session):
    result = await get_specs(session, BARE_ID, "waterproof")
    assert result is not None
    assert not result.available
    assert result.message == NOT_AVAILABLE


async def test_t04_hidden_or_unknown_product_is_not_found(session):
    assert await get_specs(session, HIDDEN_ID) is None
    assert await get_specs(session, "gid://shopify/Product/0") is None


# ---- pictures


async def test_colour_gets_its_variant_picture(session):
    result = await get_images(session, REEF_ID, colour="neela")
    assert result is not None
    assert [i.url for i in result.images] == ["https://cdn.example/reef-blue.jpg?v=7"]
    assert result.colour == "blue"
    assert result.colour_image_found is True


async def test_colour_without_its_own_picture_uses_the_featured_one(session):
    result = await get_images(session, REEF_ID, colour="black")
    assert result is not None
    assert [i.url for i in result.images] == ["https://cdn.example/reef-1.jpg?v=1"]
    assert result.colour_image_found is False


async def test_specific_variant_picture(session):
    result = await get_images(session, REEF_ID, variant_id=BLUE_VARIANT)
    assert result is not None
    assert result.images[0].source == "variant"


async def test_more_pictures_is_three_without_the_same_picture_twice(session):
    result = await get_images(session, REEF_ID, colour="blue", max_images=10)
    assert result is not None
    assert len(result.images) == 3
    assert sum("reef-blue" in i.url for i in result.images) == 1


async def test_product_without_pictures_gets_a_message_and_the_link(session):
    result = await get_images(session, BARE_ID)
    assert result is not None
    assert result.images == []
    assert result.message == NO_IMAGE_MESSAGE
    assert result.product_url == "https://shop.example/products/vantor-bare"


async def test_hidden_product_has_no_pictures_to_give(session):
    assert await get_images(session, HIDDEN_ID) is None
