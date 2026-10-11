"""WhatsApp Cloud API client: send one text message.

One function: `send_text()`. It retries a few times on timeouts, HTTP 429 and 5xx (waiting 1s,
then 2s). Any other error (400, 401, 403...) is permanent: retrying will not help, so it raises
WhatsAppError at once. The Meta error code (for example 131031) goes into `exc.detail`.
Never log the phone number or the access token.
"""

import asyncio
import logging
from typing import Any

import httpx

from app.core.errors import ConfigError, WhatsAppError, WhatsAppTemporaryError
from app.core.settings import get_settings
from app.integrations.whatsapp.payloads import build_text_payload

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # 1st try + 2 retries, waiting 1s then 2s


def _meta_error_code(response: httpx.Response) -> int | None:
    """Meta puts its own error code in {"error": {"code": ...}}."""
    try:
        return int(response.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return None


async def _post(url: str, payload: dict[str, Any], token: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15) as http:
        return await http.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})


async def send_text(to: str, body: str) -> str:
    """Send a text message and return its wamid."""
    s = get_settings()
    token = s.whatsapp_access_token.get_secret_value()
    if not (s.meta_graph_version and s.whatsapp_phone_number_id and token):
        raise ConfigError(
            "Set META_GRAPH_VERSION, WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN in .env"
        )

    url = f"https://graph.facebook.com/{s.meta_graph_version}/{s.whatsapp_phone_number_id}/messages"
    payload = build_text_payload(to, body)
    last_error = WhatsAppTemporaryError("WhatsApp request failed")

    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            log.warning("whatsapp_retry", extra={"attempt": attempt + 1, "code": last_error.code})
            await asyncio.sleep(2 ** (attempt - 1))  # 1s, then 2s

        try:
            response = await _post(url, payload, token)
        except httpx.HTTPError:
            last_error = WhatsAppTemporaryError("Could not reach WhatsApp")
            continue

        status = response.status_code
        if status == 429 or status >= 500:
            last_error = WhatsAppTemporaryError(
                f"WhatsApp temporary error (HTTP {status})",
                detail={"status": status, "meta_code": _meta_error_code(response)},
            )
            continue
        if status >= 400:
            raise WhatsAppError(
                f"WhatsApp refused the message (HTTP {status})",
                detail={"status": status, "meta_code": _meta_error_code(response)},
            )

        try:
            return str(response.json()["messages"][0]["id"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise WhatsAppError("WhatsApp answer had no message id") from exc

    raise last_error
