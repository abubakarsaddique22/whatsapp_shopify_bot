"""Download of a WhatsApp voice note (Phase 6, step 3). No network: httpx.MockTransport plays Meta.

The mock answers two kinds of calls, like Meta: the media-info call on graph.facebook.com and the
file download on another host. Each test says which answers come, in order."""

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.core.errors import (
    ConfigError,
    MediaTooLarge,
    UnsupportedMedia,
    WhatsAppError,
    WhatsAppTemporaryError,
)
from app.core.settings import Settings
from app.integrations.whatsapp import media

MAX_BYTES = 1000
AUDIO = b"x" * 500


def info(url: str = "https://lookaside.test/f", mime: str = "audio/ogg; codecs=opus", **extra):
    data = {"url": url, "mime_type": mime, "file_size": 500, **extra}
    return httpx.Response(200, json=data)


def file(content: bytes = AUDIO) -> httpx.Response:
    return httpx.Response(200, content=content)


class FakeMeta:
    """Answers the info calls and the file calls from two lists, in order."""

    def __init__(self, infos: list, files: list) -> None:
        self.infos = list(infos)
        self.files = list(files)
        self.info_calls = 0
        self.file_calls = 0
        self.auth_headers: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth_headers.append(request.headers.get("authorization", ""))
        if request.url.host == "graph.facebook.com":
            answer = self.infos[self.info_calls]
            self.info_calls += 1
        else:
            answer = self.files[self.file_calls]
            self.file_calls += 1
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(meta_graph_version="v99.0", whatsapp_access_token=SecretStr("test-token"))
    monkeypatch.setattr(media, "get_settings", lambda: settings)


def meta(monkeypatch: pytest.MonkeyPatch, infos: list, files: list) -> FakeMeta:
    server = FakeMeta(infos, files)
    transport = httpx.MockTransport(server.handler)
    monkeypatch.setattr(
        media, "_make_client", lambda timeout: httpx.AsyncClient(transport=transport)
    )
    return server


# ---- the normal path


async def test_download_writes_the_file_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = meta(monkeypatch, [info()], [file()])
    result = await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)

    assert result.path.parent == tmp_path
    assert result.path.read_bytes() == AUDIO
    assert result.size_bytes == 500
    assert result.mime_type == "audio/ogg"  # '; codecs=opus' is dropped
    assert server.auth_headers == ["Bearer test-token", "Bearer test-token"]


async def test_the_file_name_is_random_and_has_no_media_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta(monkeypatch, [info(), info()], [file(), file()])
    first = await media.download_media("MEDIA-SECRET-ID", tmp_path, max_bytes=MAX_BYTES)
    second = await media.download_media("MEDIA-SECRET-ID", tmp_path, max_bytes=MAX_BYTES)
    assert first.path != second.path
    assert "MEDIA" not in first.path.name


# ---- refused before or during the download


async def test_a_non_audio_type_is_refused_before_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = meta(monkeypatch, [info(mime="image/jpeg")], [])
    with pytest.raises(UnsupportedMedia):
        await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)
    assert server.file_calls == 0


async def test_announced_size_too_big_is_refused_before_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = meta(monkeypatch, [info(file_size=MAX_BYTES + 1)], [])
    with pytest.raises(MediaTooLarge):
        await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)
    assert server.file_calls == 0


async def test_real_size_too_big_stops_the_download_and_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The announced size says 500 but the body is 2000 bytes: the real count wins."""
    meta(monkeypatch, [info(file_size=500)], [file(b"x" * 2000)])
    with pytest.raises(MediaTooLarge):
        await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)
    assert list(tmp_path.iterdir()) == []


async def test_an_empty_body_is_an_error_and_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta(monkeypatch, [info()], [file(b"")])
    with pytest.raises(WhatsAppError):
        await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)
    assert list(tmp_path.iterdir()) == []


# ---- expired media url: refetched once, never more


async def test_an_expired_url_is_refetched_once_and_then_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = meta(
        monkeypatch,
        [info(url="https://lookaside.test/old"), info(url="https://lookaside.test/new")],
        [httpx.Response(404), file()],
    )
    result = await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)
    assert result.path.read_bytes() == AUDIO
    assert (server.info_calls, server.file_calls) == (2, 2)


async def test_a_url_refused_twice_gives_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = meta(monkeypatch, [info(), info()], [httpx.Response(404), httpx.Response(403)])
    with pytest.raises(WhatsAppError):
        await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)
    assert (server.info_calls, server.file_calls) == (2, 2)  # no third try
    assert list(tmp_path.iterdir()) == []


# ---- errors from Meta


async def test_info_server_error_is_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta(monkeypatch, [httpx.Response(500)], [])
    with pytest.raises(WhatsAppTemporaryError):
        await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)


async def test_info_rate_limit_is_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta(monkeypatch, [httpx.Response(429)], [])
    with pytest.raises(WhatsAppTemporaryError):
        await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)


async def test_an_expired_token_is_permanent_and_carries_the_meta_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = httpx.Response(401, json={"error": {"code": 190, "message": "expired"}})
    meta(monkeypatch, [refused], [])
    with pytest.raises(WhatsAppError) as caught:
        await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)
    assert not isinstance(caught.value, WhatsAppTemporaryError)
    assert caught.value.detail["meta_code"] == 190


async def test_network_error_on_info_is_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta(monkeypatch, [httpx.ConnectTimeout("slow")], [])
    with pytest.raises(WhatsAppTemporaryError):
        await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)


async def test_download_server_error_is_temporary_and_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta(monkeypatch, [info()], [httpx.Response(500)])
    with pytest.raises(WhatsAppTemporaryError):
        await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)
    assert list(tmp_path.iterdir()) == []


async def test_info_without_a_url_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta(monkeypatch, [httpx.Response(200, json={"mime_type": "audio/ogg"})], [])
    with pytest.raises(WhatsAppError):
        await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)


async def test_missing_settings_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media, "get_settings", lambda: Settings(meta_graph_version=""))
    with pytest.raises(ConfigError):
        await media.download_media("MEDIA1", tmp_path, max_bytes=MAX_BYTES)


# ---- small helpers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("audio/ogg; codecs=opus", "audio/ogg"),
        ("AUDIO/MPEG", "audio/mpeg"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_mime(raw: str | None, expected: str) -> None:
    assert media.normalize_mime(raw) == expected
