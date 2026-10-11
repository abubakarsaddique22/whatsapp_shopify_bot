"""Unit tests for integrations/whatsapp/client.py. No network: `_post` is replaced in each test."""

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.errors import ConfigError, WhatsAppError, WhatsAppTemporaryError
from app.core.settings import Settings
from app.integrations.whatsapp import client

OK = httpx.Response(200, json={"messages": [{"id": "wamid.OUT1"}]})


def _error(status: int, meta_code: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": meta_code, "message": "x"}})


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        meta_graph_version="v99.0",
        whatsapp_phone_number_id="123",
        whatsapp_access_token=SecretStr("test-token"),
    )
    monkeypatch.setattr(client, "get_settings", lambda: settings)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(client.asyncio, "sleep", no_sleep)


def _fake_post(monkeypatch: pytest.MonkeyPatch, answers: list[httpx.Response | Exception]) -> list:
    """Each call to _post returns (or raises) the next answer. Returns the list of calls."""
    calls: list[tuple[str, dict[str, Any], str]] = []

    async def fake(url: str, payload: dict[str, Any], token: str) -> httpx.Response:
        calls.append((url, payload, token))
        answer = answers[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(client, "_post", fake)
    return calls


async def test_success_returns_wamid(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_post(monkeypatch, [OK])
    assert await client.send_text("923001234567", "Received") == "wamid.OUT1"
    url, payload, token = calls[0]
    assert url == "https://graph.facebook.com/v99.0/123/messages"
    assert payload["to"] == "923001234567"
    assert payload["text"]["body"] == "Received"
    assert token == "test-token"


async def test_retries_on_500_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_post(monkeypatch, [httpx.Response(500), OK])
    assert await client.send_text("923001234567", "hi") == "wamid.OUT1"
    assert len(calls) == 2


async def test_retries_on_timeout_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_post(monkeypatch, [httpx.ConnectTimeout("slow"), OK])
    assert await client.send_text("923001234567", "hi") == "wamid.OUT1"
    assert len(calls) == 2


async def test_gives_up_after_three_attempts_on_500(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_post(monkeypatch, [httpx.Response(500)] * 3)
    with pytest.raises(WhatsAppTemporaryError):
        await client.send_text("923001234567", "hi")
    assert len(calls) == 3


async def test_429_is_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_post(monkeypatch, [httpx.Response(429)] * 3)
    with pytest.raises(WhatsAppTemporaryError):
        await client.send_text("923001234567", "hi")
    assert len(calls) == 3


async def test_400_does_not_retry_and_keeps_meta_code(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_post(monkeypatch, [_error(400, 131031)])
    with pytest.raises(WhatsAppError) as info:
        await client.send_text("923001234567", "hi")
    assert not isinstance(info.value, WhatsAppTemporaryError)
    assert info.value.detail["meta_code"] == 131031
    assert len(calls) == 1


async def test_401_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_post(monkeypatch, [_error(401, 190)])
    with pytest.raises(WhatsAppError):
        await client.send_text("923001234567", "hi")
    assert len(calls) == 1


async def test_answer_without_message_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_post(monkeypatch, [httpx.Response(200, json={})])
    with pytest.raises(WhatsAppError):
        await client.send_text("923001234567", "hi")


async def test_missing_settings_raise_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = Settings(meta_graph_version="", whatsapp_access_token=SecretStr(""))
    monkeypatch.setattr(client, "get_settings", lambda: empty)
    with pytest.raises(ConfigError):
        await client.send_text("923001234567", "hi")
