"""JPEG renditions and conversion without network or database (Phase 7, step 2)."""

import io
import os

import pytest
from PIL import Image

from app.catalog.image_formats import (
    ImageConversionError,
    jpeg_rendition_url,
    prepare_image,
    to_jpeg_bytes,
)

CDN = "https://cdn.shopify.com/s/files/1/0001/products"


def make_bytes(image: Image.Image, fmt: str) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def noise(size: int) -> Image.Image:
    return Image.frombytes("RGB", (size, size), os.urandom(size * size * 3))


# ---- rendition URL


def test_webp_url_gets_jpg_format_and_width_and_keeps_the_version():
    url = jpeg_rendition_url(f"{CDN}/watch.webp?v=123")
    assert url == f"{CDN}/watch.webp?v=123&format=jpg&width=1600"


def test_url_without_a_query_works_too():
    assert jpeg_rendition_url(f"{CDN}/watch.avif") == f"{CDN}/watch.avif?format=jpg&width=1600"


def test_jpg_and_png_urls_stay_as_they_are():
    for name in ("watch.jpg", "watch.JPEG", "watch.png"):
        url = f"{CDN}/{name}?v=1"
        assert jpeg_rendition_url(url) == url


def test_other_hosts_stay_as_they_are():
    url = "https://example.com/watch.webp?v=1"
    assert jpeg_rendition_url(url) == url


def test_an_old_format_value_is_replaced_not_repeated():
    url = jpeg_rendition_url(f"{CDN}/watch.webp?format=webp&v=2")
    assert url.count("format=") == 1
    assert "format=jpg" in url
    assert "v=2" in url


def test_an_existing_width_is_respected():
    url = jpeg_rendition_url(f"{CDN}/watch.webp?width=800")
    assert "width=800" in url
    assert url.count("width=") == 1


# ---- conversion with Pillow


def test_webp_becomes_a_jpeg():
    webp = make_bytes(Image.new("RGB", (200, 100), (20, 120, 220)), "WEBP")
    result = to_jpeg_bytes(webp)
    assert result[:2] == b"\xff\xd8"  # JPEG signature
    with Image.open(io.BytesIO(result)) as opened:
        assert opened.format == "JPEG"
        assert opened.size == (200, 100)


def test_big_picture_is_shrunk_to_the_longest_side():
    big = make_bytes(Image.new("RGB", (4000, 3000), (200, 30, 30)), "PNG")
    with Image.open(io.BytesIO(to_jpeg_bytes(big, max_side=1600))) as opened:
        assert max(opened.size) == 1600
        assert opened.size == (1600, 1200)  # the shape stays the same


def test_transparent_picture_gets_a_white_background():
    clear = make_bytes(Image.new("RGBA", (20, 20), (0, 0, 0, 0)), "PNG")
    with Image.open(io.BytesIO(to_jpeg_bytes(clear))) as opened:
        assert opened.mode == "RGB"
        assert all(channel > 240 for channel in opened.getpixel((10, 10)))


def test_unreadable_data_raises():
    with pytest.raises(ImageConversionError):
        to_jpeg_bytes(b"this is not a picture")


def test_picture_that_cannot_fit_raises():
    with pytest.raises(ImageConversionError):
        to_jpeg_bytes(make_bytes(noise(600), "PNG"), max_bytes=5_000)


# ---- prepare_image (what the media upload will call)


def test_small_jpeg_is_returned_unchanged():
    jpeg = make_bytes(Image.new("RGB", (50, 50), (1, 2, 3)), "JPEG")
    assert prepare_image(jpeg, "image/jpeg") == (jpeg, "image/jpeg")


def test_small_png_is_returned_unchanged_and_the_mime_is_cleaned():
    png = make_bytes(Image.new("RGB", (50, 50), (1, 2, 3)), "PNG")
    assert prepare_image(png, "IMAGE/PNG; charset=binary") == (png, "image/png")


def test_webp_is_converted_to_jpeg():
    webp = make_bytes(Image.new("RGB", (50, 50), (9, 9, 9)), "WEBP")
    data, mime = prepare_image(webp, "image/webp")
    assert mime == "image/jpeg"
    assert data[:2] == b"\xff\xd8"


def test_png_over_the_size_limit_becomes_a_smaller_jpeg():
    png = make_bytes(noise(300), "PNG")  # about 270 KB
    assert len(png) > 250_000
    data, mime = prepare_image(png, "image/png", max_bytes=250_000)
    assert mime == "image/jpeg"
    assert len(data) <= 250_000
