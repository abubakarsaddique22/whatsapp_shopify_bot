"""Shopify GraphQL client (Phase 2): retries, throttling, token refresh after a 401, read-only.

No real network: respx plays Shopify. The token provider and asyncio.sleep are replaced, so no
test waits and no test needs a secret.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.core.errors import ShopifyAuthError, ShopifyError, ShopifyThrottled, ShopifyUnavailable
from app.core.settings import Settings
from app.integrations.shopify import auth, client

URL = "https://test-shop.myshopify.com/admin/api/2026-10/graphql.json"
QUERY = "{ shop { name } }"
OK = {"data": {"shop": {"name": "Test"}}}
THROTTLED = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    settings = Settings(shopify_shop_domain="test-shop", shopify_api_version="2026-10")
    monkeypatch.setattr(client, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)  # shop_host() lives in auth

    issued: list[str] = []
    forgotten: list[bool] = []
    sleeps: list[float] = []

    async def fake_token() -> str:
        token = f"tok-{len(issued) + 1}"
        issued.append(token)
        return token

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(client, "get_token", fake_token)
    monkeypatch.setattr(client, "forget_token", lambda: forgotten.append(True))
    monkeypatch.setattr(client, "asyncio", SimpleNamespace(sleep=fake_sleep))
    return SimpleNamespace(issued=issued, forgotten=forgotten, sleeps=sleeps)


# ---- a normal answer


async def test_the_data_part_of_the_answer_is_returned(env: SimpleNamespace) -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json=OK))
        data = await client.graphql(QUERY, {"x": 1})
    assert data == {"shop": {"name": "Test"}}
    request = route.calls[0].request
    assert request.headers["x-shopify-access-token"] == "tok-1"
    body = json.loads(request.content)
    assert body == {"query": QUERY, "variables": {"x": 1}}
    assert env.sleeps == []


async def test_a_null_data_part_gives_an_empty_dict(env: SimpleNamespace) -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": None}))
        assert await client.graphql(QUERY) == {}


# ---- throttling and server errors are retried, with a limit


async def test_throttled_then_success(env: SimpleNamespace) -> None:
    with respx.mock:
        route = respx.post(URL).mock(
            side_effect=[httpx.Response(200, json=THROTTLED), httpx.Response(200, json=OK)]
        )
        data = await client.graphql(QUERY)
    assert data == OK["data"]
    assert route.call_count == 2
    assert env.sleeps == [1]


async def test_always_throttled_gives_up_after_three_tries(env: SimpleNamespace) -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json=THROTTLED))
        with pytest.raises(ShopifyThrottled):
            await client.graphql(QUERY)
    assert route.call_count == client.MAX_ATTEMPTS == 3
    assert env.sleeps == [1, 2]  # waits 1 s, then 2 s


async def test_http_429_is_throttling_too(env: SimpleNamespace) -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(429))
        with pytest.raises(ShopifyThrottled):
            await client.graphql(QUERY)
    assert route.call_count == 3


async def test_a_server_error_is_retried_then_reported(env: SimpleNamespace) -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(503))
        with pytest.raises(ShopifyUnavailable):
            await client.graphql(QUERY)
    assert route.call_count == 3


async def test_a_server_error_then_success(env: SimpleNamespace) -> None:
    with respx.mock:
        respx.post(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200, json=OK)])
        assert await client.graphql(QUERY) == OK["data"]


@pytest.mark.parametrize("error", [httpx.ConnectError("boom"), httpx.ReadTimeout("slow")])
async def test_network_errors_are_retried_then_reported(
    env: SimpleNamespace, error: httpx.HTTPError
) -> None:
    with respx.mock:
        route = respx.post(URL).mock(side_effect=error)
        with pytest.raises(ShopifyUnavailable):
            await client.graphql(QUERY)
    assert route.call_count == 3


# ---- a 401 gets one fresh token, never more


async def test_a_401_gets_a_new_token_and_one_more_try(env: SimpleNamespace) -> None:
    with respx.mock:
        route = respx.post(URL).mock(
            side_effect=[httpx.Response(401), httpx.Response(200, json=OK)]
        )
        data = await client.graphql(QUERY)
    assert data == OK["data"]
    assert env.forgotten == [True]  # the old token was dropped
    assert [c.request.headers["x-shopify-access-token"] for c in route.calls] == [
        "tok-1",
        "tok-2",
    ]


async def test_a_second_401_is_an_auth_error(env: SimpleNamespace) -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(401))
        with pytest.raises(ShopifyAuthError):
            await client.graphql(QUERY)
    assert route.call_count == 2
    assert env.forgotten == [True]  # refreshed once, not twice


async def test_a_403_is_an_auth_error_without_retry(env: SimpleNamespace) -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(403))
        with pytest.raises(ShopifyAuthError):
            await client.graphql(QUERY)
    assert route.call_count == 1


# ---- errors that must not be retried


async def test_other_http_errors_are_not_retried(env: SimpleNamespace) -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(400))
        with pytest.raises(ShopifyError) as caught:
            await client.graphql(QUERY)
    assert caught.value.code == "shopify_error"
    assert route.call_count == 1


async def test_an_answer_that_is_not_json_is_an_error(env: SimpleNamespace) -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
        with pytest.raises(ShopifyError, match="not JSON"):
            await client.graphql(QUERY)


async def test_a_graphql_error_is_reported_without_retry(env: SimpleNamespace) -> None:
    body = {"errors": [{"message": "Field 'nope' doesn't exist"}]}
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(ShopifyError, match="doesn't exist"):
            await client.graphql(QUERY)
    assert route.call_count == 1


# ---- read-only


@pytest.mark.parametrize(
    "query",
    ['mutation { productDelete(input: {id: "x"}) { deletedProductId } }', "  MUTATION Foo { a }"],
)
async def test_mutations_are_refused_before_any_request(env: SimpleNamespace, query: str) -> None:
    with respx.mock(assert_all_called=False):
        route = respx.post(URL).mock(return_value=httpx.Response(200, json=OK))
        with pytest.raises(ShopifyError, match="read-only"):
            await client.graphql(query)
    assert route.call_count == 0
    assert env.issued == []  # not even a token was fetched
