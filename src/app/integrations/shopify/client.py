"""Read-only Shopify GraphQL client.

One function: `graphql()`. It adds the access token, retries a few times on timeouts,
throttling and 5xx errors, gets a fresh token once after a 401, and refuses any mutation
(the bot is read-only).
"""

import asyncio
import logging
import re

import httpx

from app.core.errors import ShopifyAuthError, ShopifyError, ShopifyThrottled, ShopifyUnavailable
from app.core.settings import get_settings
from app.integrations.shopify.auth import forget_token, get_token, shop_host

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # 1st try + 2 retries, waiting 1s then 2s
_MUTATION = re.compile(r"\bmutation\b", re.IGNORECASE)


def _is_throttled(errors: object) -> bool:
    if not isinstance(errors, list):
        return False
    return any(
        isinstance(e, dict) and (e.get("extensions") or {}).get("code") == "THROTTLED"
        for e in errors
    )


def _error_text(errors: object) -> str:
    if isinstance(errors, list):
        parts = [str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors]
        return "; ".join(parts)[:300]
    return str(errors)[:300]


async def _post(url: str, payload: dict) -> httpx.Response:
    headers = {"X-Shopify-Access-Token": await get_token()}
    async with httpx.AsyncClient(timeout=15) as http:
        return await http.post(url, json=payload, headers=headers)


async def graphql(query: str, variables: dict | None = None) -> dict:
    """Run a read-only GraphQL query and return the 'data' part of the answer."""
    if _MUTATION.search(query):
        raise ShopifyError("Mutations are not allowed: this bot is read-only")

    url = f"https://{shop_host()}/admin/api/{get_settings().shopify_api_version}/graphql.json"
    payload = {"query": query, "variables": variables or {}}
    refreshed = False
    last_error: ShopifyError = ShopifyUnavailable("Shopify request failed")

    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            log.warning("shopify_retry", extra={"attempt": attempt + 1, "code": last_error.code})
            await asyncio.sleep(2 ** (attempt - 1))  # 1s, then 2s

        try:
            response = await _post(url, payload)
        except httpx.HTTPError:
            last_error = ShopifyUnavailable("Could not reach Shopify")
            continue

        status = response.status_code
        if status == 401 and not refreshed:
            forget_token()  # token may have expired or been revoked: get a new one and retry
            refreshed = True
            last_error = ShopifyAuthError("Shopify rejected the access token")
            continue
        if status in (401, 403):
            raise ShopifyAuthError(f"Shopify refused the request (HTTP {status})")
        if status == 429:
            last_error = ShopifyThrottled("Shopify rate limit (HTTP 429)")
            continue
        if status >= 500:
            last_error = ShopifyUnavailable(f"Shopify server error (HTTP {status})")
            continue
        if status != 200:
            raise ShopifyError(f"Shopify returned HTTP {status}")

        try:
            body = response.json()
        except ValueError as exc:
            raise ShopifyError("Shopify returned a response that is not JSON") from exc

        errors = body.get("errors")
        if errors:
            if _is_throttled(errors):
                last_error = ShopifyThrottled("Shopify GraphQL cost limit (THROTTLED)")
                continue
            raise ShopifyError(f"Shopify GraphQL error: {_error_text(errors)}")

        return body.get("data") or {}

    raise last_error
