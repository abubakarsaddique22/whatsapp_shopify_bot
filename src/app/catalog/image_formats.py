"""JPEG renditions of product pictures (Phase 7, step 2; Blueprint 4.10 and the Phase 7 card).

WhatsApp accepts only JPEG and PNG images, up to about 5 MB (VERIFY the exact limit on Meta's
docs). Shopify may store WebP or AVIF. Two ways to get a safe picture:

1. `jpeg_rendition_url()`: ask the Shopify CDN for a JPEG version through URL parameters
   (`format=jpg`, `width=...`). No download needed. VERIFY once on the real store (see README).
2. `prepare_image()` / `to_jpeg_bytes()`: we already have the bytes (downloaded for a media
   upload), so Pillow converts them. Also shrinks a picture that is too big.

Nothing here touches the network or the database.
"""

import io
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PIL import Image

WHATSAPP_MIME_TYPES = frozenset({"image/jpeg", "image/png"})
WHATSAPP_MAX_BYTES = 5_000_000  # VERIFY against Meta's current media limits
MAX_SIDE = 1600  # longest side in pixels: plenty for a phone screen, keeps files small

_QUALITIES = (85, 75, 65, 55)  # tried in this order until the file fits
_SAFE_EXTENSIONS = (".jpg", ".jpeg", ".png")
_SHOPIFY_CDN_HOSTS = frozenset({"cdn.shopify.com"})


class ImageConversionError(ValueError):
    """The picture could not be read, or is still too large after compression."""


def jpeg_rendition_url(url: str, max_side: int = MAX_SIDE) -> str:
    """Shopify CDN URL that returns a JPEG. Other URLs and .jpg/.png files stay unchanged.

    The existing query (for example '?v=123') is kept. An old 'format=' value is replaced, and
    'width=' is only added when neither 'width' nor 'height' is already there.
    """
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in _SHOPIFY_CDN_HOSTS:
        return url
    if parts.path.lower().endswith(_SAFE_EXTENSIONS):
        return url

    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "format"]
    query.append(("format", "jpg"))
    if not any(k in ("width", "height") for k, _ in query):
        query.append(("width", str(max_side)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _flatten(image: Image.Image) -> Image.Image:
    """RGB copy. Transparent areas become white (JPEG has no transparency)."""
    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    if has_alpha:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def to_jpeg_bytes(
    data: bytes, *, max_bytes: int = WHATSAPP_MAX_BYTES, max_side: int = MAX_SIDE
) -> bytes:
    """Convert any picture Pillow can read to a JPEG of at most `max_bytes`.

    Raises ImageConversionError for unreadable data (including formats this Pillow build cannot
    open) or if even the lowest quality is too big.
    """
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = _flatten(source)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageConversionError("Could not read the picture") from exc

    image.thumbnail((max_side, max_side))
    for quality in _QUALITIES:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() <= max_bytes:
            return buffer.getvalue()
    raise ImageConversionError("Picture is still too large after compression")


def prepare_image(
    data: bytes, mime: str, *, max_bytes: int = WHATSAPP_MAX_BYTES
) -> tuple[bytes, str]:
    """Return (bytes, mime) that WhatsApp will accept.

    A JPEG or PNG within the size limit is returned as it is. Anything else (WebP, AVIF, GIF) or
    anything too big becomes a JPEG.
    """
    clean_mime = mime.split(";", 1)[0].strip().lower()
    if clean_mime in WHATSAPP_MIME_TYPES and len(data) <= max_bytes:
        return data, clean_mime
    return to_jpeg_bytes(data, max_bytes=max_bytes), "image/jpeg"
