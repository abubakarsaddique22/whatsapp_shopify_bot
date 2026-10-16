"""Shopify token provider (Phase 2): cache, refresh, errors, scope audit.

No real network: respx plays Shopify. The token and the client secret are fake values.
"""

import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.core.errors import ConfigError, ForbiddenScope, ShopifyAuthError, ShopifyUnavailable
from app.core.settings import Settings
from app.integrations.shopify import auth

URL = "https://test-shop.myshopify.com/admin/oauth/access_token"
SECRET = "client-secret-value"


def make_settings(**over: object) -> Settings:
    values: dict[str, object] = {
        "shopify_shop_domain": "test-shop",
        "shopify_client_id": "client-id",
        "shopify_client_secret": SecretStr(SECRET),
        "shopify_auth_mode": "client_credentials",
    }
    values.update(over)
    return Settings(**values)


def token_body(token: str = "tok-1", scope: str = "read_products,read_inventory", expires_in=86399):
    return {"access_token": token, "scope": scope, "expires_in": expires_in}


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch):
    """Every test starts with no cached token, a new lock and fake settings."""
    monkeypatch.setattr(auth, "get_settings", lambda: make_settings())
    monkeypatch.setattr(auth, "_lock", asyncio.Lock())
    auth.forget_token()
    yield
    auth.forget_token()


# ---- the shop name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("test-shop", "test-shop.myshopify.com"),
        ("https://Test-Shop.myshopify.com/", "test-shop.myshopify.com"),
        ("  test-shop.myshopify.com ", "test-shop.myshopify.com"),
    ],
)
def test_shop_host_is_normalised(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
    monkeypatch.setattr(auth, "get_settings", lambda: make_settings(shopify_shop_domain=raw))
    assert auth.shop_host() == expected


def test_an_empty_shop_domain_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "get_settings", lambda: make_settings(shopify_shop_domain=" "))
    with pytest.raises(ConfigError):
        auth.shop_host()


# ---- the token is cached and refreshed


async def test_the_token_is_requested_once_and_then_cached() -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json=token_body()))
        assert await auth.get_token() == "tok-1"
        assert await auth.get_token() == "tok-1"
    assert route.call_count == 1


async def test_the_request_is_a_client_credentials_grant() -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json=token_body()))
        await auth.get_token()
    body = route.calls[0].request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=client-id" in body


async def test_the_token_is_refreshed_shortly_before_it_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(auth, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    with respx.mock:
        route = respx.post(URL).mock(
            side_effect=[
                httpx.Response(200, json=token_body("tok-1", expires_in=100)),
                httpx.Response(200, json=token_body("tok-2", expires_in=100)),
            ]
        )
        assert await auth.get_token() == "tok-1"
        clock["now"] = 1030.0  # 70 seconds left: still fine
        assert await auth.get_token() == "tok-1"
        clock["now"] = 1050.0  # 50 seconds left: inside the 60 second margin
        assert await auth.get_token() == "tok-2"
    assert route.call_count == 2


async def test_forget_token_forces_a_new_request() -> None:
    with respx.mock:
        route = respx.post(URL).mock(
            side_effect=[
                httpx.Response(200, json=token_body("tok-1")),
                httpx.Response(200, json=token_body("tok-2")),
            ]
        )
        assert await auth.get_token() == "tok-1"
        auth.forget_token()  # what client.py does after a 401
        assert await auth.get_token() == "tok-2"
    assert route.call_count == 2


async def test_ten_parallel_calls_make_one_request() -> None:
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json=token_body()))
        tokens = await asyncio.gather(*[auth.get_token() for _ in range(10)])
    assert set(tokens) == {"tok-1"}
    assert route.call_count == 1


# ---- errors


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, {"error": "invalid_request"}, ShopifyAuthError),
        (401, {"error": "invalid_client"}, ShopifyAuthError),
        (429, {}, ShopifyUnavailable),
        (500, {}, ShopifyUnavailable),
        (503, {}, ShopifyUnavailable),
    ],
)
async def test_token_endpoint_errors(status: int, body: dict, expected: type[Exception]) -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(status, json=body))
        with pytest.raises(expected):
            await auth.get_token()
    assert auth._token is None  # nothing half-cached


async def test_shop_not_permitted_explains_the_organization_rule() -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(400, json={"error": "shop_not_permitted"}))
        with pytest.raises(ShopifyAuthError, match="same Shopify organization"):
            await auth.get_token()


async def test_a_network_error_is_shopify_unavailable() -> None:
    with respx.mock:
        respx.post(URL).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(ShopifyUnavailable):
            await auth.get_token()


@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, json={"no": "token"}), httpx.Response(200, text="not json")],
)
async def test_an_unexpected_answer_is_an_auth_error(response: httpx.Response) -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=response)
        with pytest.raises(ShopifyAuthError):
            await auth.get_token()


async def test_a_forbidden_scope_stops_the_bot_and_the_token_is_not_kept() -> None:
    with respx.mock:
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, json=token_body(scope="read_products,read_orders"))
        )
        with pytest.raises(ForbiddenScope):
            await auth.get_token()
        assert auth._token is None
        with pytest.raises(ForbiddenScope):  # asked again, refused again
            await auth.get_token()
    assert route.call_count == 2


async def test_missing_client_id_or_secret_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth, "get_settings", lambda: make_settings(shopify_client_id=""))
    with pytest.raises(ConfigError):
        await auth.get_token()
    monkeypatch.setattr(
        auth, "get_settings", lambda: make_settings(shopify_client_secret=SecretStr(""))
    )
    with pytest.raises(ConfigError):
        await auth.get_token()


async def test_only_client_credentials_mode_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth, "get_settings", lambda: make_settings(shopify_auth_mode="offline_token")
    )
    with pytest.raises(ConfigError):
        await auth.get_token()


# ---- secrets never leak


async def test_secrets_and_tokens_never_reach_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with respx.mock:
        respx.post(URL).mock(
            side_effect=[
                httpx.Response(200, json=token_body("tok-secret-1")),
                httpx.Response(401, json={"error": "invalid_client"}),
            ]
        )
        await auth.get_token()
        auth.forget_token()
        with pytest.raises(ShopifyAuthError) as caught:
            await auth.get_token()
    assert SECRET not in caplog.text
    assert "tok-secret-1" not in caplog.text
    assert SECRET not in str(caught.value)
