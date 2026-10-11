"""Tool-level tests for agent/tools.py (Phase 3, step 7) on the real Postgres.

Same method as test_search.py and test_specs_images.py: made-up 'Zorbex' and 'Vantor' products
inside a transaction that is rolled back, so the real mirror is never touched.

Price and stock come from Shopify, so their happy path runs against the REAL dev store and only
when asked for:   $env:RUN_LIVE="1"; uv run pytest tests\\integration\\test_tools.py -v
Only the 'Shopify is down' case (T15) uses a simulated error. Every other test below returns
before any Shopify call, and a guard makes the test fail if one is made by mistake.
"""

import os
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.agent import tools
from app.agent.tools import _resolve
from app.catalog import stock as stock_mod
from app.catalog.importer import compute_hash, image_rows, product_row, variant_rows
from app.catalog.price import format_money
from app.core.errors import ShopifyUnavailable
from app.core.settings import get_settings
from app.db import repositories as repo
from app.domain.catalog import Image, Money, Product, StockState, Variant
from app.integrations.shopify import gateway

live = pytest.mark.skipif(not os.getenv("RUN_LIVE"), reason="set RUN_LIVE=1 to use the real store")


def make_variant(
    no: int, n: int, colour: str, size: str, price: str, image: str | None = None
) -> Variant:
    return Variant(
        id=f"gid://shopify/ProductVariant/{no}0{n}",
        product_id=f"gid://shopify/Product/{no}",
        title=f"{colour} / {size}",
        price=Money(amount=Decimal(price), currency="PKR"),
        options={"Colour": colour, "Case size": size},
        available_for_sale=True,
        inventory_quantity=3,
        stock=StockState.IN_STOCK,
        image_url=image,
    )


def make_product(
    no: int, title: str, variants: list[Variant], vendor: str = "Zorbex", **extra
) -> Product:
    return Product(
        id=f"gid://shopify/Product/{no}",
        title=title,
        handle=title.lower().replace(" ", "-"),
        url=f"https://shop.example/products/{no}",
        vendor=vendor,
        variants=variants,
        **extra,
    )


TIDE = make_product(9001, "Zorbex Tide", [make_variant(9001, 1, "Black", "40mm", "9500")])
TIDE_ULTRA = make_product(
    9002, "Zorbex Tide Ultra", [make_variant(9002, 1, "Blue", "44mm", "15000")]
)

REEF_ID = "gid://shopify/Product/9101"
BLACK_40 = "gid://shopify/ProductVariant/910101"
BLUE_44 = "gid://shopify/ProductVariant/910102"
REEF = make_product(
    9101,
    "Vantor Reef",
    [
        make_variant(9101, 1, "Black", "40mm", "9000"),
        make_variant(9101, 2, "Blue", "44mm", "9000", "https://cdn.example/reef-blue.jpg?v=7"),
    ],
    vendor="Vantor",
    specs={"water_resistance": "200 m"},
    images=[
        Image(url="https://cdn.example/reef-1.jpg?v=1"),
        Image(url="https://cdn.example/reef-blue.jpg?v=5"),
        Image(url="https://cdn.example/reef-3.jpg"),
    ],
)

BARE_ID = "gid://shopify/Product/9102"
BARE_VARIANT = "gid://shopify/ProductVariant/910201"
BARE = Product(  # one variant, no colour or size option, no specs, no pictures
    id=BARE_ID,
    title="Vantor Bare",
    handle="vantor-bare",
    url="https://shop.example/products/vantor-bare",
    variants=[
        Variant(
            id=BARE_VARIANT,
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

HIDDEN_ID = "gid://shopify/Product/9103"
HIDDEN = REEF.model_copy(
    update={"id": HIDDEN_ID, "title": "Vantor Ghost", "handle": "vantor-ghost", "variants": []}
)


async def upsert(db: AsyncSession, product: Product, visible: bool = True) -> None:
    row = product_row(product, compute_hash(product)) | {"is_visible": visible}
    await repo.upsert_product(db, row, variant_rows(product), image_rows(product))


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            for product in (TIDE, TIDE_ULTRA, REEF, BARE):
                await upsert(db, product)
            await upsert(db, HIDDEN, visible=False)
            yield db
            await db.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def no_shopify(monkeypatch):
    """Fail the test if a tool calls Shopify when it should have answered from the mirror."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("this tool call must not reach Shopify")

    monkeypatch.setattr(stock_mod, "fetch_live_variant", forbidden)


# ---- every result has the audit fields


async def test_result_carries_tool_name_source_id_and_time(session):
    result = await tools.tool_get_specs(session, REEF_ID)
    assert result.tool == "get_specs"
    assert result.source_id.startswith("get_specs:")
    assert result.fetched_at.tzinfo is not None


async def test_registry_lists_the_six_tools():
    assert sorted(tools.TOOLS) == [
        "check_stock",
        "get_colors_sizes",
        "get_price",
        "get_product_image",
        "get_specs",
        "search_products",
    ]


# ---- search_products: T13 and T14


async def test_t13_ambiguous_search_returns_candidates_and_no_price(session):
    result = await tools.tool_search_products(session, "zorbex tide")
    assert result.ok
    assert result.data["kind"] == "ambiguous"
    titles = [c["title"] for c in result.data["candidates"]]
    assert titles[:2] == ["Zorbex Tide", "Zorbex Tide Ultra"]
    assert all("price" not in c for c in result.data["candidates"])  # price is live only


async def test_search_colour_in_roman_urdu_narrows_to_one(session):
    result = await tools.tool_search_products(session, "zorbex tide kala")
    assert result.data["kind"] == "one"
    assert result.data["colour"] == "black"
    assert result.data["candidates"][0]["title"] == "Zorbex Tide"


async def test_t14_unknown_product_is_ok_with_kind_none(session):
    result = await tools.tool_search_products(session, "qwertyuiop flurb")
    assert result.ok
    assert result.data["kind"] == "none"
    assert result.data["candidates"] == []


async def test_t14_hidden_product_is_never_found(session):
    result = await tools.tool_search_products(session, "vantor ghost")
    assert all(c["title"] != "Vantor Ghost" for c in result.data["candidates"])


async def test_search_rejects_empty_and_out_of_range_input(session):
    empty = await tools.tool_search_products(session, "   ")
    assert (empty.ok, empty.error_code) == (False, "invalid_input")
    huge = await tools.tool_search_products(session, "x" * 500)
    assert huge.error_code == "invalid_input"
    bad_size = await tools.tool_search_products(session, "zorbex", size_mm=5)
    assert bad_size.error_code == "invalid_input"


# ---- get_colors_sizes


async def test_colours_and_sizes_come_from_the_variants(session):
    result = await tools.tool_get_colors_sizes(session, REEF_ID)
    assert result.ok
    assert result.data["colours"] == ["black", "blue"]
    assert result.data["sizes_mm"] == [40, 44]
    assert result.data["stock_checked"] is False
    assert {(v["colour"], v["size_mm"]) for v in result.data["variants"]} == {
        ("black", 40),
        ("blue", 44),
    }


async def test_product_without_colour_option_does_not_claim_colours(session):
    result = await tools.tool_get_colors_sizes(session, BARE_ID)
    assert result.data["colours"] == []
    assert result.data["colours_listed"] is False


async def test_options_of_hidden_or_unknown_product_are_not_found(session):
    assert (await tools.tool_get_colors_sizes(session, HIDDEN_ID)).error_code == "not_found"
    unknown = await tools.tool_get_colors_sizes(session, "gid://shopify/Product/1")
    assert unknown.error_code == "not_found"


async def test_ids_that_are_not_shopify_ids_are_rejected(session):
    for call in (
        tools.tool_get_colors_sizes(session, "not-an-id"),
        tools.tool_get_specs(session, "gid://shopify/ProductVariant/1"),
        tools.tool_get_product_image(session, "'; DROP TABLE products_mirror; --"),
        tools.tool_get_price(session, REEF_ID, variant_id="gid://shopify/Product/9101"),
    ):
        result = await call
        assert (result.ok, result.error_code) == (False, "invalid_input")


# ---- get_specs: T04


async def test_t04_specs_value_comes_from_the_metafield(session):
    result = await tools.tool_get_specs(session, REEF_ID, "waterproof?")
    assert result.ok
    assert result.data["available"] is True
    assert [(s["name"], s["value"]) for s in result.data["specs"]] == [
        ("water_resistance", "200 m")
    ]


async def test_t04_missing_spec_is_not_available_not_a_guess(session):
    result = await tools.tool_get_specs(session, BARE_ID, "waterproof")
    assert result.ok
    assert result.data["available"] is False
    assert result.data["message"]


async def test_t04_hidden_product_specs_are_not_found(session):
    assert (await tools.tool_get_specs(session, HIDDEN_ID)).error_code == "not_found"


# ---- get_product_image


async def test_image_for_a_colour_is_that_variants_picture(session):
    result = await tools.tool_get_product_image(session, REEF_ID, colour="neela")
    assert result.ok
    assert [i["url"] for i in result.data["images"]] == ["https://cdn.example/reef-blue.jpg?v=7"]


async def test_image_never_gives_more_than_three(session):
    result = await tools.tool_get_product_image(session, REEF_ID, max_images=10)
    assert len(result.data["images"]) <= 3


async def test_product_without_pictures_gets_a_message_and_the_link(session):
    result = await tools.tool_get_product_image(session, BARE_ID)
    assert result.ok
    assert result.data["images"] == []
    assert result.data["message"]
    assert result.data["product_url"] == "https://shop.example/products/vantor-bare"


async def test_image_of_hidden_product_is_not_found(session):
    assert (await tools.tool_get_product_image(session, HIDDEN_ID)).error_code == "not_found"


# ---- picking the variant for price and stock (no Shopify call happens in any of these)


async def test_several_variants_means_ask_the_customer(session, no_shopify):
    for tool in (tools.tool_get_price, tools.tool_check_stock):
        result = await tool(session, REEF_ID)
        assert result.ok
        assert result.data["match"] == "ambiguous"
        assert {v["variant_id"] for v in result.data["variants"]} == {BLACK_40, BLUE_44}


async def test_colour_the_model_does_not_come_in_is_not_out_of_stock(session, no_shopify):
    result = await tools.tool_check_stock(session, REEF_ID, colour="red")
    assert result.ok
    assert result.data["reason"] == "colour_not_offered"
    assert result.data["message"] == stock_mod.STOCK_MESSAGES[StockState.COLOUR_NOT_OFFERED]
    assert result.data["colours_offered"] == ["black", "blue"]


async def test_size_and_combination_not_offered(session, no_shopify):
    size = await tools.tool_get_price(session, REEF_ID, size_mm=50)
    assert size.data["reason"] == "size_not_offered"
    combo = await tools.tool_get_price(session, REEF_ID, colour="black", size_mm=44)
    assert combo.data["reason"] == "combination_not_offered"


async def test_colour_word_not_among_the_variants_is_not_offered(session, no_shopify):
    # colour_of_value keeps unknown names as they are (a merchant may sell 'Navy'), so a word
    # like 'zzzz' is checked against the variants and simply is not there.
    result = await tools.tool_check_stock(session, REEF_ID, colour="zzzz")
    assert result.data["reason"] == "colour_not_offered"


async def test_blank_colour_is_unknown_not_a_claim(session, no_shopify):
    result = await tools.tool_check_stock(session, REEF_ID, colour="   ")
    assert result.data["reason"] == "colour_unknown"


async def test_variant_of_another_product_is_not_found(session, no_shopify):
    result = await tools.tool_get_price(session, REEF_ID, variant_id=BARE_VARIANT)
    assert (result.ok, result.error_code) == (False, "not_found")


async def test_hidden_product_has_no_price_or_stock(session, no_shopify):
    for tool in (tools.tool_get_price, tools.tool_check_stock):
        assert (await tool(session, HIDDEN_ID)).error_code == "not_found"


async def test_colour_and_size_pick_exactly_one_variant(session):
    by_colour = await _resolve(session, REEF_ID, variant_id=None, colour="kala", size_mm=None)
    assert by_colour.variant is not None and by_colour.variant.id == BLACK_40
    by_size = await _resolve(session, REEF_ID, variant_id=None, colour=None, size_mm=44)
    assert by_size.variant is not None and by_size.variant.id == BLUE_44


async def test_colour_on_a_product_without_colour_option_is_flagged_unverified(session):
    res = await _resolve(session, BARE_ID, variant_id=None, colour="black", size_mm=None)
    assert res.variant is not None and res.variant.id == BARE_VARIANT
    assert res.colour_unverified is True  # the bot must not say 'yes, it is black'


# ---- T15: Shopify is down


async def test_t15_shopify_down_stock_is_unknown_never_a_guess(session, monkeypatch):
    async def down(*args, **kwargs):
        raise ShopifyUnavailable("simulated outage")

    monkeypatch.setattr(stock_mod, "fetch_live_variant", down)
    result = await tools.tool_check_stock(session, REEF_ID, variant_id=BLUE_44)
    assert (result.ok, result.error_code) == (False, "shopify_unavailable")
    assert result.data["stock"]["state"] == StockState.UNKNOWN.value
    assert result.data["stock"]["message"] == stock_mod.STOCK_MESSAGES[StockState.UNKNOWN]
    assert result.data["stock"]["quantity"] is None


async def test_t15_shopify_down_price_has_no_value_at_all(session, monkeypatch):
    async def down(*args, **kwargs):
        raise ShopifyUnavailable("simulated outage")

    monkeypatch.setattr(stock_mod, "fetch_live_variant", down)
    result = await tools.tool_get_price(session, REEF_ID, variant_id=BLUE_44)
    assert (result.ok, result.error_code) == (False, "shopify_unavailable")
    assert result.data is None  # no old or mirror price is ever offered instead


# ---- live price and stock on the REAL dev store (RUN_LIVE=1)


@pytest.fixture
async def real_product(session) -> Product:
    products = await gateway.list_products()
    assert products, "the dev store has no active, published products"
    product = products[0]
    await upsert(session, product)  # into the rolled-back transaction only
    return product


@live
async def test_live_price_matches_shopify(session, real_product):
    variant = real_product.variants[0]
    result = await tools.tool_get_price(session, real_product.id, variant_id=variant.id)
    assert result.ok, result.error_code
    assert result.data["variant_id"] == variant.id
    assert result.data["price"]["display"] == format_money(variant.price)
    assert result.data["price"]["amount"] == str(variant.price.amount.quantize(Decimal("0.01")))


@live
async def test_live_stock_state_is_a_known_state(session, real_product):
    variant = real_product.variants[0]
    result = await tools.tool_check_stock(session, real_product.id, variant_id=variant.id)
    assert result.ok, result.error_code
    assert result.data["stock"]["state"] in {s.value for s in StockState}
    assert result.data["stock"]["message"]
    if not get_settings().show_exact_stock:
        assert result.data["stock"]["quantity"] is None  # exact counts only if the client allows
