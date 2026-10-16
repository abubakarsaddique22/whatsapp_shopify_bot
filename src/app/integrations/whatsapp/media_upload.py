"""Phase 7, step 3: the second way to send a picture, by uploaded media id.

The first way is a public link (client.send_image(link=...)): Meta downloads the picture itself.
If Meta refuses the link, the sender falls back to this module:

    data, mime = await fetch_and_prepare(url)   # download from the Shopify CDN, make it a JPEG
    media_id = await upload_image(data, mime)   # POST /{phone_number_id}/media
    await send_image(to, media_id=media_id)

`fetch_and_prepare` asks the CDN for a JPEG rendition and then runs Pillow on the bytes
(catalog/image_formats.py), so AVIF, WebP and over-large pictures all end up as a JPEG that
WhatsApp accepts. A picture that cannot be fetched or converted raises WhatsAppError or
ImageConversionError and the sender sends the plain-text fallback instead.

Never log the access token, the phone number or the picture bytes.
"""

import logging

import httpx

from app.catalog.image_formats import jpeg_rendition_url, prepare_image
from app.core.errors import ConfigError, WhatsAppError, WhatsAppTemporaryError
from app.core.settings import get_settings
from app.integrations.whatsapp.client import _meta_error_code

log = logging.getLogger(__name__)

MAX_FETCH_BYTES = 15_000_000  # a rendition is far smaller; this only stops something odd


def _make_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True)


async def fetch_and_prepare(
    url: str, *, max_bytes: int = MAX_FETCH_BYTES, timeout: float = 20.0
) -> tuple[bytes, str]:
    """Download a product picture and return (bytes, mime) that WhatsApp accepts."""
    try:
        async with _make_client(timeout) as http:
            response = await http.get(jpeg_rendition_url(url))
    except httpx.HTTPError:
        raise WhatsAppError("Could not fetch the product picture") from None
    if response.status_code >= 400:
        raise WhatsAppError(
            f"Product picture not available (HTTP {response.status_code})",
            detail={"status": response.status_code},
        )
    data = response.content
    if not data or len(data) > max_bytes:
        raise WhatsAppError("Product picture is empty or too large")
    return prepare_image(data, response.headers.get("content-type", ""))


async def upload_image(data: bytes, mime: str, *, timeout: float = 30.0) -> str:
    """Upload a JPEG or PNG to WhatsApp and return its media id."""
    s = get_settings()
    token = s.whatsapp_access_token.get_secret_value()
    if not (s.meta_graph_version and s.whatsapp_phone_number_id and token):
        raise ConfigError(
            "Set META_GRAPH_VERSION, WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN in .env"
        )

    url = f"https://graph.facebook.com/{s.meta_graph_version}/{s.whatsapp_phone_number_id}/media"
    extension = "png" if mime == "image/png" else "jpg"
    try:
        async with _make_client(timeout) as http:
            response = await http.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (f"picture.{extension}", data, mime)},
            )
    except httpx.HTTPError:
        raise WhatsAppTemporaryError("Could not reach WhatsApp (media upload)") from None

    status = response.status_code
    if status == 429 or status >= 500:
        raise WhatsAppTemporaryError(
            f"media upload: WhatsApp temporary error (HTTP {status})",
            detail={"status": status, "meta_code": _meta_error_code(response)},
        )
    if status >= 400:
        raise WhatsAppError(
            f"media upload: WhatsApp refused the picture (HTTP {status})",
            detail={"status": status, "meta_code": _meta_error_code(response)},
        )
    try:
        return str(response.json()["id"])
    except (ValueError, KeyError, TypeError):
        raise WhatsAppError("WhatsApp upload answer had no media id") from None
