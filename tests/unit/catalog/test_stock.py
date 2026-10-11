"""Stock wording, colour lookup and the live variant cache (Phase 3, step 3)."""

import pytest

from app.catalog import stock
from app.core.errors import ShopifyUnavailable
from app.domain.catalog import StockState, Variant
from tests.factories import make_variant


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (StockState.IN_STOCK, "Available."),
        (StockState.LOW_STOCK, "Limited stock."),
        (StockState.OUT_OF_STOCK, "Currently out of stock."),
        (
            StockState.BACKORDER,
            "Can be ordered, but delivery may take longer; please confirm with the team.",
        ),
        (StockState.UNKNOWN, "I cannot check stock right now."),
    ],
)
def test_every_state_has_the_blueprint_wording(state: StockState, message: str):
    info = stock.stock_info(make_variant(stock=state), show_exact=False)
    assert info.state == state
    assert info.message == message


def test_colour_not_offered_is_different_from_out_of_stock():
    not_offered = stock.colour_not_offered()
    out = stock.stock_info(make_variant(stock=StockState.OUT_OF_STOCK), show_exact=False)
    assert not_offered.state == StockState.COLOUR_NOT_OFFERED
    assert not_offered.message == "This model does not come in that colour."
    assert not_offered.message != out.message


def test_unknown_stock_never_guesses():
    info = stock.unknown_stock()
    assert info.state == StockState.UNKNOWN
    assert info.quantity is None


def test_exact_count_hidden_by_default():
    info = stock.stock_info(make_variant(inventory_quantity=10), show_exact=False)
    assert info.quantity is None


def test_exact_count_shown_only_when_allowed_and_really_in_stock():
    assert stock.stock_info(make_variant(inventory_quantity=10), show_exact=True).quantity == 10
    low = make_variant(stock=StockState.LOW_STOCK, inventory_quantity=2)
    assert stock.stock_info(low, show_exact=True).quantity == 2


@pytest.mark.parametrize(
    "variant",
    [
        make_variant(stock=StockState.OUT_OF_STOCK, inventory_quantity=0),
        make_variant(stock=StockState.BACKORDER, inventory_quantity=0),
        make_variant(stock=StockState.IN_STOCK, inventory_quantity=0),  # not tracked
        make_variant(stock=StockState.IN_STOCK, inventory_quantity=None),
        make_variant(stock=StockState.UNKNOWN, inventory_quantity=5),
    ],
)
def test_exact_count_never_invented(variant: Variant):
    assert stock.stock_info(variant, show_exact=True).quantity is None


def test_variants_with_colour_understands_roman_urdu():
    black = make_variant(id="v1", options={"Colour": "Black"})
    blue = make_variant(id="v2", options={"Colour": "Blue"})
    assert stock.variants_with_colour([black, blue], "kala") == [black]
    assert stock.variants_with_colour([black, blue], "Blue") == [blue]
    assert stock.variants_with_colour([black, blue], "red") == []
    assert stock.variants_with_colour([black, blue], "") == []


def test_variants_with_colour_works_with_the_color_spelling():
    black = make_variant(options={"Color": "Matte Black"})
    assert stock.variants_with_colour([black], "black") == [black]


# ---- live lookup


@pytest.fixture(autouse=True)
def _clean_cache():
    stock.clear_live_cache()
    yield
    stock.clear_live_cache()


class FakeGateway:
    """Stands in for gateway.get_variant and counts the calls."""

    def __init__(self, variant: Variant | None) -> None:
        self.variant = variant
        self.calls = 0

    async def get_variant(self, variant_id: str) -> Variant | None:
        self.calls += 1
        return self.variant


async def test_no_cache_by_default_every_call_goes_to_shopify(monkeypatch):
    fake = FakeGateway(make_variant())
    monkeypatch.setattr(stock.gateway, "get_variant", fake.get_variant)
    await stock.fetch_live_variant("v1", ttl_seconds=0)
    await stock.fetch_live_variant("v1", ttl_seconds=0)
    assert fake.calls == 2


async def test_cache_reuses_the_answer_until_it_expires(monkeypatch):
    fake = FakeGateway(make_variant())
    clock = [100.0]
    monkeypatch.setattr(stock.gateway, "get_variant", fake.get_variant)
    monkeypatch.setattr(stock, "_now", lambda: clock[0])

    await stock.fetch_live_variant("v1", ttl_seconds=5)
    clock[0] = 103.0
    await stock.fetch_live_variant("v1", ttl_seconds=5)
    assert fake.calls == 1  # still fresh

    clock[0] = 106.0
    await stock.fetch_live_variant("v1", ttl_seconds=5)
    assert fake.calls == 2  # expired, asked Shopify again


async def test_missing_variant_is_none_and_not_cached(monkeypatch):
    fake = FakeGateway(None)
    monkeypatch.setattr(stock.gateway, "get_variant", fake.get_variant)
    assert await stock.fetch_live_variant("v1", ttl_seconds=5) is None
    assert await stock.fetch_live_variant("v1", ttl_seconds=5) is None
    assert fake.calls == 2


async def test_shopify_errors_are_not_hidden_here(monkeypatch):
    async def down(variant_id: str) -> Variant | None:
        raise ShopifyUnavailable("Could not reach Shopify")

    monkeypatch.setattr(stock.gateway, "get_variant", down)
    with pytest.raises(ShopifyUnavailable):
        await stock.fetch_live_variant("v1", ttl_seconds=0)
