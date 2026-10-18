"""Tool-level T13 (ambiguous query) and T14 (no match) for search, on the real Postgres.

Each test works inside a transaction that is rolled back at the end, with made-up 'Zorbex'
products, so the real mirror is never touched and real products cannot disturb the results.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.catalog.importer import compute_hash, image_rows, product_row, variant_rows
from app.catalog.search import MatchKind, search_products
from app.core.settings import get_settings
from app.db import repositories as repo
from app.domain.catalog import Money, Product, StockState, Variant


def make_variant(product_no: int, n: int, colour: str, size: str, price: str) -> Variant:
    return Variant(
        id=f"gid://shopify/ProductVariant/{product_no}0{n}",
        product_id=f"gid://shopify/Product/{product_no}",
        title=f"{colour} / {size}",
        price=Money(amount=Decimal(price), currency="PKR"),
        options={"Colour": colour, "Case size": size},
        available_for_sale=True,
        inventory_quantity=3,
        stock=StockState.IN_STOCK,
    )


def make_product(no: int, title: str, variants: list[Variant]) -> Product:
    return Product(
        id=f"gid://shopify/Product/{no}",
        title=title,
        handle=title.lower().replace(" ", "-"),
        url=f"https://shop.example/products/{no}",
        vendor="Zorbex",
        variants=variants,
    )


TIDE = make_product(  # black 40mm 9,500
    9001, "Zorbex Tide", [make_variant(9001, 1, "Black", "40mm", "9500")]
)
TIDE_ULTRA = make_product(  # blue 44mm 15,000
    9002, "Zorbex Tide Ultra", [make_variant(9002, 1, "Blue", "44mm", "15000")]
)
GA = make_product(9003, "Zorbex GA-2100", [make_variant(9003, 1, "Black", "44mm", "21000")])


def plain_product(no: int, title: str) -> Product:
    """Single-variant product with no colour option, like most products of the real store."""
    variant = Variant(
        id=f"gid://shopify/ProductVariant/{no}01",
        product_id=f"gid://shopify/Product/{no}",
        title="Default Title",
        price=Money(amount=Decimal("8000"), currency="PKR"),
        options={"Title": "Default Title"},
        available_for_sale=True,
        inventory_quantity=3,
        stock=StockState.IN_STOCK,
    )
    return make_product(no, title, [variant]).model_copy(update={"vendor": "Quillon"})


PLAIN = plain_product(9004, "Quillon Plain")  # no colour anywhere
PLAIN_BLACK = plain_product(9005, "Quillon Plain Black")  # colour only in the name

# Two products that share most words; colour words are part of the TITLE (like the real store)
VEXOR_A = plain_product(9006, "Vexor Silver Gold Black Textured - AAA").model_copy(
    update={"vendor": "Vexor"}
)
VEXOR_B = plain_product(9007, "Vexor Silver Green Textured - AAA").model_copy(
    update={"vendor": "Vexor"}
)
# Two products with the very same title: the title alone cannot tell them apart
DUPE_A = plain_product(9008, "Dupe Watch").model_copy(update={"vendor": "Dupe"})
DUPE_B = plain_product(9009, "Dupe Watch").model_copy(update={"vendor": "Dupe"})


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            for product in (
                TIDE,
                TIDE_ULTRA,
                GA,
                PLAIN,
                PLAIN_BLACK,
                VEXOR_A,
                VEXOR_B,
                DUPE_A,
                DUPE_B,
            ):
                await repo.upsert_product(
                    db,
                    product_row(product, compute_hash(product)),
                    variant_rows(product),
                    image_rows(product),
                )
            yield db
            await db.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


async def test_t13_close_matches_are_ambiguous_and_name_the_candidates(session):
    result = await search_products(session, "zorbex tide")
    assert result.kind == MatchKind.AMBIGUOUS
    assert [c.title for c in result.candidates][:2] == ["Zorbex Tide", "Zorbex Tide Ultra"]


async def test_colour_narrows_to_one_product(session):
    result = await search_products(session, "zorbex tide", colour="blue")
    assert result.kind == MatchKind.ONE
    assert [c.title for c in result.candidates] == ["Zorbex Tide Ultra"]


async def test_roman_urdu_colour_word_in_the_query_is_understood(session):
    result = await search_products(session, "zorbex tide kala")
    assert result.colour == "black"
    assert result.candidates[0].title == "Zorbex Tide"
    assert "Zorbex Tide Ultra" not in [c.title for c in result.candidates]  # it is blue
    assert result.kind == MatchKind.ONE


async def test_price_ceiling_and_size_filters(session):
    cheap = await search_products(session, "zorbex tide", max_price=10000)
    assert cheap.candidates[0].title == "Zorbex Tide"
    assert "Zorbex Tide Ultra" not in [c.title for c in cheap.candidates]  # 15,000
    big = await search_products(session, "zorbex tide", size_mm=44)
    assert big.candidates[0].title == "Zorbex Tide Ultra"
    assert "Zorbex Tide" not in [c.title for c in big.candidates]  # 40mm only


async def test_spaced_dashed_and_glued_model_codes_all_match(session):
    for query in ("zorbex ga2100", "zorbex ga 2100", "ZORBEX GA-2100"):
        result = await search_products(session, query)
        assert [c.title for c in result.candidates][:1] == ["Zorbex GA-2100"], query
        assert result.kind == MatchKind.ONE, query


async def test_colour_the_model_does_not_come_in_keeps_the_name_match(session):
    result = await search_products(session, "zorbex ga2100", colour="red")
    assert result.kind == MatchKind.NONE
    assert result.candidates == []
    assert result.name_matches >= 1  # the product exists; only the colour is missing


async def test_colour_only_in_the_name_is_confirmed_and_ranked_first(session):
    result = await search_products(session, "quillon plain", colour="black")
    titles = [c.title for c in result.candidates]
    assert titles[0] == "Quillon Plain Black"
    assert result.candidates[0].colour_verified is True
    plain = next(c for c in result.candidates if c.title == "Quillon Plain")
    assert plain.colour_verified is False  # unverified: shown, but the bot must not claim it


async def test_name_with_a_different_colour_is_excluded(session):
    result = await search_products(session, "quillon plain", colour="blue")
    titles = [c.title for c in result.candidates]
    assert "Quillon Plain Black" not in titles
    assert "Quillon Plain" in titles  # nothing says its colour: not hidden


async def test_unverified_colour_still_respects_the_price_ceiling(session):
    result = await search_products(session, "quillon plain", colour="black", max_price=5000)
    assert result.candidates == []  # both cost 8,000


async def test_t14_unknown_product_is_none(session):
    result = await search_products(session, "qwertyuiop flurb")
    assert result.kind == MatchKind.NONE
    assert result.candidates == []
    assert result.name_matches == 0


async def test_t14_hidden_product_behaves_like_a_missing_one(session):
    hidden = product_row(GA, compute_hash(GA)) | {"is_visible": False}
    await repo.upsert_product(session, hidden, variant_rows(GA), image_rows(GA))
    result = await search_products(session, "zorbex ga2100")
    assert all(c.title != "Zorbex GA-2100" for c in result.candidates)


async def test_limit_is_never_more_than_five(session):
    result = await search_products(session, "zorbex", limit=50)
    assert len(result.candidates) <= 5


async def test_full_title_with_colour_words_finds_that_one_product(session):
    # Found by the eval (Phase 12): the colour words used to be cut out of the query, the rest
    # matched both Vexor products equally and the bot asked 'which one?'.
    for query in (
        "Vexor Silver Gold Black Textured - AAA",
        "vexor silver gold black textured aaa",
    ):
        result = await search_products(session, query)
        assert result.kind == MatchKind.ONE, query
        assert [c.title for c in result.candidates] == [VEXOR_A.title], query


async def test_part_of_a_title_with_colour_words_still_lists_both_products(session):
    result = await search_products(session, "vexor textured aaa")
    titles = [c.title for c in result.candidates]
    assert VEXOR_A.title in titles
    assert VEXOR_B.title in titles


async def test_two_products_with_the_same_title_stay_ambiguous(session):
    result = await search_products(session, "Dupe Watch")
    assert result.kind == MatchKind.AMBIGUOUS
    titles = [c.title for c in result.candidates]
    assert titles.count("Dupe Watch") == 2  # both are offered, the bot asks which one
