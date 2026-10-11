"""Shopify access token via the client credentials grant (read-only bot).

The token lasts about 24 hours. We cache it in memory and ask for a new one shortly before it
expires, so callers just do `await get_token()`. Secrets and tokens are never logged.
"""

import asyncio
import logging
import time

import httpx

from app.core.errors import ConfigError, ShopifyAuthError, ShopifyUnavailable
from app.core.security import audit_scopes
from app.core.settings import get_settings

log = logging.getLogger(__name__)

_REFRESH_MARGIN_SECONDS = 60

_token: str | None = None
_expires_at: float = 0.0  # time.monotonic() value
_lock = asyncio.Lock()


def shop_host() -> str:
    """'zg1apf-2p' or 'zg1apf-2p.myshopify.com' -> 'zg1apf-2p.myshopify.com'."""
    shop = get_settings().shopify_shop_domain.strip().lower()
    shop = shop.removeprefix("https://").removeprefix("http://").rstrip("/")
    if not shop:
        raise ConfigError("SHOPIFY_SHOP_DOMAIN is empty")
    if not shop.endswith(".myshopify.com"):
        shop += ".myshopify.com"
    return shop


async def get_token() -> str:
    """Return a valid access token, fetching a new one if needed."""
    global _token, _expires_at
    async with _lock:
        if _token and time.monotonic() < _expires_at - _REFRESH_MARGIN_SECONDS:
            return _token
        _token, expires_in = await _request_token()
        _expires_at = time.monotonic() + expires_in
        return _token


def forget_token() -> None:
    """Drop the cached token (client.py calls this after a 401, then asks for a fresh one)."""
    global _token, _expires_at
    _token, _expires_at = None, 0.0


def _error_code(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", "unknown"))
    except (ValueError, AttributeError):
        return "unknown"


async def _request_token() -> tuple[str, int]:
    settings = get_settings()
    if settings.shopify_auth_mode != "client_credentials":
        raise ConfigError("Only SHOPIFY_AUTH_MODE=client_credentials is supported")
    client_secret = settings.shopify_client_secret.get_secret_value()
    if not settings.shopify_client_id or not client_secret:
        raise ConfigError("SHOPIFY_CLIENT_ID or SHOPIFY_CLIENT_SECRET is missing")

    url = f"https://{shop_host()}/admin/oauth/access_token"
    form = {
        "grant_type": "client_credentials",
        "client_id": settings.shopify_client_id,
        "client_secret": client_secret,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            response = await http.post(url, data=form)
    except httpx.HTTPError as exc:
        raise ShopifyUnavailable("Could not reach Shopify to get a token") from exc

    if response.status_code == 429 or response.status_code >= 500:
        raise ShopifyUnavailable(f"Shopify token endpoint returned {response.status_code}")
    if response.status_code != 200:
        error = _error_code(response)
        hint = ""
        if error == "shop_not_permitted":
            hint = " (the app and the store must be in the same Shopify organization)"
        raise ShopifyAuthError(f"Token request failed: {response.status_code} {error}{hint}")

    try:
        data = response.json()
        token = data["access_token"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ShopifyAuthError("Unexpected token response from Shopify") from exc

    # The bot must stay read-only: refuse to run if the app has any other scope.
    granted = {s.strip() for s in str(data.get("scope", "")).split(",") if s.strip()}
    audit_scopes(granted)

    expires_in = int(data.get("expires_in", 86399))
    log.info("shopify_token_refreshed", extra={"expires_in": expires_in})
    return token, expires_in
