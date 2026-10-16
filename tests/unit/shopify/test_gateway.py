"""Shopify gateway (Phase 2): pagination, hidden products, id checks. No network.

graphql() is replaced by a function that plays Shopify's pages, so these tests run anywhere.
"""

import pytest

from app.core.errors import ShopifyError, ValidationFailed
from app.core.settings import Settings
from app.integrations.shopify import gateway

from .test_mapper import product


def page(ids: list[str], *, has_next: bool, cursor: str | None) -> dict:
    return {
        "products": {
            "nodes": [{"id": i} for i in ids],
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        }
    }


async def test_all_three_pages_are_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        None: page(["1", "2"], has_next=True, cursor="c1"),
        "c1": page(["3"], has_next=True, cursor="c2"),
        "c2": page(["4"], has_next=False, cursor="c3"),
    }
    seen: list[str | None] = []

    async def fake_graphql(query: str, variables: dict | None = None) -> dict:
        assert variables is not None
        seen.append(variables["after"])
        return pages[variables["after"]]

    monkeypatch.setattr(gateway, "graphql", fake_graphql)

    _, nodes = await gateway._all_nodes("query", "products", {"first": 2})

    assert [n["id"] for n in nodes] == ["1", "2", "3", "4"]
    assert seen == [None, "c1", "c2"]  # each call continued from the last cursor


async def test_a_page_that_says_more_but_has_no_cursor_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_graphql(query: str, variables: dict | None = None) -> dict:
        nonlocal calls
        calls += 1
        return page(["1"], has_next=True, cursor=None)

    monkeypatch.setattr(gateway, "graphql", fake_graphql)
    _, nodes = await gateway._all_nodes("query", "products", {})
    assert calls == 1
    assert len(nodes) == 1


async def test_pagination_has_a_safety_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_graphql(query: str, variables: dict | None = None) -> dict:
        nonlocal calls
        calls += 1
        return page(["x"], has_next=True, cursor=f"c{calls}")

    monkeypatch.setattr(gateway, "graphql", fake_graphql)
    monkeypatch.setattr(gateway, "MAX_PAGES", 5)
    with pytest.raises(ShopifyError, match="Stopped after 5 pages"):
        await gateway._all_nodes("query", "products", {})
    assert calls == 5


async def test_list_products_drops_hidden_products_and_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = product()
    draft = product(id="gid://shopify/Product/2", status="DRAFT")
    unpublished = product(id="gid://shopify/Product/3", onlineStoreUrl=None)
    data = {
        "shop": {"currencyCode": "PKR"},
        "products": {
            "nodes": [active, draft, unpublished, active],  # active appears twice
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    }

    async def fake_graphql(query: str, variables: dict | None = None) -> dict:
        return data

    monkeypatch.setattr(gateway, "graphql", fake_graphql)
    monkeypatch.setattr(gateway, "load_specs_mapping", lambda: {})
    monkeypatch.setattr(gateway, "get_settings", lambda: Settings(low_stock_threshold=3))

    products = await gateway.list_products()

    assert [p.id for p in products] == [active["id"]]
    assert products[0].variants[0].price.currency == "PKR"


@pytest.mark.parametrize(
    "bad_id",
    ["", "123", "gid://shopify/Product/", "gid://shopify/Product/1; drop", "https://evil/x"],
)
async def test_an_invalid_product_id_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch, bad_id: str
) -> None:
    async def must_not_be_called(*args: object, **kwargs: object) -> dict:
        raise AssertionError("Shopify must not be called with an invalid id")

    monkeypatch.setattr(gateway, "graphql", must_not_be_called)
    with pytest.raises(ValidationFailed):
        await gateway.get_product(bad_id)


async def test_a_product_id_is_not_accepted_as_a_variant_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def must_not_be_called(*args: object, **kwargs: object) -> dict:
        raise AssertionError("Shopify must not be called with an invalid id")

    monkeypatch.setattr(gateway, "graphql", must_not_be_called)
    with pytest.raises(ValidationFailed):
        await gateway.get_variant("gid://shopify/Product/1")
