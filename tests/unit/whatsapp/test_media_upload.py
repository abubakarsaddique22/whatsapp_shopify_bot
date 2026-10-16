"""Downloading and uploading a product picture (Phase 7, step 3). Fake HTTP, no network."""

import io
from collections.abc import Callable

import httpx
import pytest
from PIL import Image

from app.catalog.image_formats import ImageConversionError
from app.core.errors import ConfigError, WhatsAppError, WhatsAppTemporaryError
from app.core.settings import Settings
from app.integrations.whatsapp import media_upload

CDN_URL = "https://cdn.shopify.com/s/files/1/1/files/reef.webp?v=1"


def picture(fmt: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (80, 60), (10, 90, 200)).save(buffer, format=fmt)
    return buffer.getvalue()


def use_http(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        media_upload,
        "_make_client",
        lambda timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
    )


def use_settings(monkeypatch: pytest.MonkeyPatch, token: str = "tok") -> None:
    monkeypatch.setattr(
        media_upload,
        "get_settings",
        lambda: Settings(
            meta_graph_version="v99.0",
            whatsapp_phone_number_id="555",
            whatsapp_access_token=token,
        ),
    )


# ---- fetch_and_prepare


async def test_webp_is_fetched_as_a_jpeg_rendition_and_stays_a_jpeg(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=picture("WEBP"), headers={"content-type": "image/webp"})

    use_http(monkeypatch, handler)
    data, mime = await media_upload.fetch_and_prepare(CDN_URL)

    assert "format=jpg" in seen[0]
    assert mime == "image/jpeg"
    assert data[:2] == b"\xff\xd8"


async def test_a_jpeg_from_the_cdn_is_passed_on_unchanged(monkeypatch: pytest.MonkeyPatch):
    jpeg = picture("JPEG")
    use_http(
        monkeypatch,
        lambda request: httpx.Response(200, content=jpeg, headers={"content-type": "image/jpeg"}),
    )
    assert await media_upload.fetch_and_prepare(CDN_URL) == (jpeg, "image/jpeg")


async def test_picture_not_found_raises_whatsapp_error(monkeypatch: pytest.MonkeyPatch):
    use_http(monkeypatch, lambda request: httpx.Response(404))
    with pytest.raises(WhatsAppError):
        await media_upload.fetch_and_prepare(CDN_URL)


async def test_empty_answer_raises_whatsapp_error(monkeypatch: pytest.MonkeyPatch):
    use_http(monkeypatch, lambda request: httpx.Response(200, content=b""))
    with pytest.raises(WhatsAppError):
        await media_upload.fetch_and_prepare(CDN_URL)


async def test_network_failure_raises_whatsapp_error(monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    use_http(monkeypatch, handler)
    with pytest.raises(WhatsAppError):
        await media_upload.fetch_and_prepare(CDN_URL)


async def test_a_file_that_is_not_a_picture_raises_conversion_error(
    monkeypatch: pytest.MonkeyPatch,
):
    use_http(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=b"not a picture", headers={"content-type": "image/webp"}
        ),
    )
    with pytest.raises(ImageConversionError):
        await media_upload.fetch_and_prepare(CDN_URL)


# ---- upload_image


async def test_upload_returns_the_media_id(monkeypatch: pytest.MonkeyPatch):
    use_settings(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "MEDIA1"})

    use_http(monkeypatch, handler)
    media_id = await media_upload.upload_image(picture("JPEG"), "image/jpeg")

    assert media_id == "MEDIA1"
    request = requests[0]
    assert request.url.path == "/v99.0/555/media"
    assert request.headers["authorization"] == "Bearer tok"
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b"messaging_product" in request.content


async def test_upload_refused_raises_whatsapp_error_with_the_meta_code(
    monkeypatch: pytest.MonkeyPatch,
):
    use_settings(monkeypatch)
    use_http(monkeypatch, lambda request: httpx.Response(400, json={"error": {"code": 131053}}))

    with pytest.raises(WhatsAppError) as caught:
        await media_upload.upload_image(b"x", "image/jpeg")

    assert not isinstance(caught.value, WhatsAppTemporaryError)
    assert caught.value.detail["meta_code"] == 131053


async def test_upload_server_error_is_temporary(monkeypatch: pytest.MonkeyPatch):
    use_settings(monkeypatch)
    use_http(monkeypatch, lambda request: httpx.Response(503))
    with pytest.raises(WhatsAppTemporaryError):
        await media_upload.upload_image(b"x", "image/jpeg")


async def test_upload_answer_without_an_id_raises(monkeypatch: pytest.MonkeyPatch):
    use_settings(monkeypatch)
    use_http(monkeypatch, lambda request: httpx.Response(200, json={"oops": 1}))
    with pytest.raises(WhatsAppError):
        await media_upload.upload_image(b"x", "image/jpeg")


async def test_upload_without_settings_raises_config_error(monkeypatch: pytest.MonkeyPatch):
    use_settings(monkeypatch, token="")
    with pytest.raises(ConfigError):
        await media_upload.upload_image(b"x", "image/jpeg")
