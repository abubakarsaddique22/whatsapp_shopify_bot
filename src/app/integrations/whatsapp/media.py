"""Phase 6, step 3: download one WhatsApp media file (a voice note) to a temporary file.

    media = await download_media(media_id, folder, max_bytes=5_000_000)
    media.path, media.mime_type, media.size_bytes

How Meta does it (two calls, both with the access token):
1. GET /{version}/{media_id}      -> {"url": ..., "mime_type": ..., "file_size": ...}
2. GET <url>                      -> the bytes. The url works for about 5 minutes only.

Rules (blueprint 6.9, 5.x):
- Only audio types are accepted (UnsupportedMedia otherwise; the file is not downloaded).
- Too big is stopped twice: by the announced file_size before the download, and by counting the
  real bytes while downloading (a wrong announced size cannot fill the disk). MediaTooLarge.
- The file gets a random name inside `folder`; a half-written file is deleted at once on any
  error. The CALLER deletes the finished file (voice/convert.py does it in a finally block).
- If the media url was already expired (HTTP 401, 403 or 404), the url is fetched once more and
  the download is tried once more, never more. (VERIFY against Meta's current behaviour.)
- Timeouts, 429 and 5xx raise WhatsAppTemporaryError (the worker retries the message); any
  other refusal raises WhatsAppError.
- Never log or store the url, the token or the audio.
"""

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.errors import (
    ConfigError,
    MediaTooLarge,
    UnsupportedMedia,
    WhatsAppError,
    WhatsAppTemporaryError,
)
from app.core.settings import get_settings
from app.integrations.whatsapp.client import _meta_error_code

log = logging.getLogger(__name__)

# Voice notes from WhatsApp are audio/ogg (opus). The rest are normal audio files.
ALLOWED_AUDIO_TYPES = frozenset(
    {
        "audio/ogg",
        "audio/opus",
        "audio/mpeg",
        "audio/mp4",
        "audio/aac",
        "audio/amr",
        "audio/wav",
        "audio/x-wav",
    }
)
EXPIRED_STATUSES = frozenset({401, 403, 404})  # the media url no longer works
CHUNK = 64 * 1024


@dataclass(frozen=True)
class MediaInfo:
    url: str
    mime_type: str
    file_size: int | None


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    mime_type: str
    size_bytes: int


class _UrlExpired(Exception):
    """Internal: the media url was refused as expired. Never leaves this module."""


def _make_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def normalize_mime(mime_type: str | None) -> str:
    """'audio/ogg; codecs=opus' -> 'audio/ogg'."""
    return (mime_type or "").split(";")[0].strip().lower()


def _check_info(info: MediaInfo, max_bytes: int) -> None:
    if normalize_mime(info.mime_type) not in ALLOWED_AUDIO_TYPES:
        raise UnsupportedMedia("media type is not audio", detail={"mime_type": info.mime_type})
    if info.file_size is not None and info.file_size > max_bytes:
        raise MediaTooLarge("media is larger than the allowed size")


def _raise_for_error(response: httpx.Response, what: str) -> None:
    status = response.status_code
    if status == 429 or status >= 500:
        raise WhatsAppTemporaryError(
            f"{what}: WhatsApp temporary error (HTTP {status})",
            detail={"status": status, "meta_code": _meta_error_code(response)},
        )
    if status >= 400:
        raise WhatsAppError(
            f"{what}: WhatsApp refused the request (HTTP {status})",
            detail={"status": status, "meta_code": _meta_error_code(response)},
        )


async def _fetch_info(
    http: httpx.AsyncClient, media_id: str, version: str, token: str
) -> MediaInfo:
    try:
        response = await http.get(
            f"https://graph.facebook.com/{version}/{media_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError:
        raise WhatsAppTemporaryError("Could not reach WhatsApp (media info)") from None
    _raise_for_error(response, "media info")
    try:
        data = response.json()
        url = str(data["url"])
        mime_type = str(data.get("mime_type", ""))
        raw_size = data.get("file_size")
        file_size = int(raw_size) if raw_size is not None else None
    except (ValueError, KeyError, TypeError):
        raise WhatsAppError("WhatsApp media info had no url") from None
    return MediaInfo(url=url, mime_type=mime_type, file_size=file_size)


async def _download(
    http: httpx.AsyncClient, info: MediaInfo, folder: Path, token: str, max_bytes: int
) -> DownloadedMedia:
    path = folder / f"{uuid.uuid4().hex}.bin"  # random name: no customer data in the file name
    size = 0
    try:
        async with http.stream(
            "GET", info.url, headers={"Authorization": f"Bearer {token}"}
        ) as response:
            if response.status_code in EXPIRED_STATUSES:
                raise _UrlExpired
            if response.status_code >= 400:
                await response.aread()
                _raise_for_error(response, "media download")
            with path.open("wb") as out:
                async for chunk in response.aiter_bytes(CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise MediaTooLarge("media is larger than the allowed size")
                    out.write(chunk)
    except httpx.HTTPError:
        path.unlink(missing_ok=True)
        raise WhatsAppTemporaryError("Could not download the media from WhatsApp") from None
    except BaseException:
        path.unlink(missing_ok=True)  # never leave a half-written file behind
        raise
    if size == 0:
        path.unlink(missing_ok=True)
        raise WhatsAppError("WhatsApp media was empty")
    return DownloadedMedia(path=path, mime_type=normalize_mime(info.mime_type), size_bytes=size)


async def download_media(
    media_id: str, folder: Path, *, max_bytes: int, timeout: float = 20.0
) -> DownloadedMedia:
    """Download one audio file into `folder`. The caller deletes the file when it is done."""
    settings = get_settings()
    token = settings.whatsapp_access_token.get_secret_value()
    if not (settings.meta_graph_version and token):
        raise ConfigError("Set META_GRAPH_VERSION and WHATSAPP_ACCESS_TOKEN in .env")
    version = settings.meta_graph_version

    async with _make_client(timeout) as http:
        info = await _fetch_info(http, media_id, version, token)
        _check_info(info, max_bytes)
        try:
            return await _download(http, info, folder, token, max_bytes)
        except _UrlExpired:
            log.info("media_url_expired_refetching")
        # the url expired: ask for a fresh one, once, and try once more
        info = await _fetch_info(http, media_id, version, token)
        _check_info(info, max_bytes)
        try:
            return await _download(http, info, folder, token, max_bytes)
        except _UrlExpired:
            raise WhatsAppError("WhatsApp media url was refused twice") from None
