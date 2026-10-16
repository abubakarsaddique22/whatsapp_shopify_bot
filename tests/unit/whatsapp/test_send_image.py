"""Sending a picture: the payload and the client (Phase 7, step 3). No network."""

from typing import Any

import httpx
import pytest

from app.core.errors import ConfigError, WhatsAppError, WhatsAppTemporaryError
from app.core.settings import Settings
from app.integrations.whatsapp import client
from app.integrations.whatsapp.payloads import build_image_payload

LINK = "https://cdn.shopify.com/s/files/1/1/files/reef.webp?v=1&format=jpg&width=1600"


def settings(**override: str) -> Settings:
    values = {
        "meta_graph_version": "v99.0",
        "whatsapp_phone_number_id": "555",
        "whatsapp_access_token": "tok",
    }
    return Settings(**(values | override))


# ---- payload


def test_image_payload_by_link_with_caption():
    payload = build_image_payload("92300", link=LINK, caption="Vantor Reef")
    assert payload == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "92300",
        "type": "image",
        "image": {"link": LINK, "caption": "Vantor Reef"},
    }


def test_image_payload_by_media_id_has_no_empty_caption():
    payload = build_image_payload("92300", media_id="MEDIA1", caption="")
    assert payload["image"] == {"id": "MEDIA1"}


def test_image_payload_needs_exactly_one_source():
    with pytest.raises(ValueError):
        build_image_payload("92300")
    with pytest.raises(ValueError):
        build_image_payload("92300", link=LINK, media_id="MEDIA1")


# ---- client


def fake_post(monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]) -> list[dict]:
    calls: list[dict[str, Any]] = []

    async def post(url: str, payload: dict[str, Any], token: str) -> httpx.Response:
        calls.append({"url": url, "payload": payload, "token": token})
        return responses[min(len(calls), len(responses)) - 1]

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(client, "_post", post)
    monkeypatch.setattr(client.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(client, "get_settings", settings)
    return calls


async def test_send_image_by_link_returns_the_wamid(monkeypatch: pytest.MonkeyPatch):
    calls = fake_post(monkeypatch, [httpx.Response(200, json={"messages": [{"id": "wamid.IMG"}]})])

    wamid = await client.send_image("92300", link=LINK, caption="Vantor Reef")

    assert wamid == "wamid.IMG"
    assert len(calls) == 1
    assert calls[0]["url"] == "https://graph.facebook.com/v99.0/555/messages"
    assert calls[0]["token"] == "tok"
    assert calls[0]["payload"]["image"] == {"link": LINK, "caption": "Vantor Reef"}


async def test_a_refused_picture_is_not_retried(monkeypatch: pytest.MonkeyPatch):
    refused = httpx.Response(400, json={"error": {"code": 131053}})
    calls = fake_post(monkeypatch, [refused])

    with pytest.raises(WhatsAppError) as caught:
        await client.send_image("92300", link=LINK)

    assert not isinstance(caught.value, WhatsAppTemporaryError)
    assert caught.value.detail["meta_code"] == 131053
    assert len(calls) == 1


async def test_a_server_error_is_retried_then_raised_as_temporary(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = fake_post(monkeypatch, [httpx.Response(500, json={})])

    with pytest.raises(WhatsAppTemporaryError):
        await client.send_image("92300", media_id="MEDIA1")

    assert len(calls) == client.MAX_ATTEMPTS


async def test_missing_whatsapp_settings_raise_config_error(monkeypatch: pytest.MonkeyPatch):
    fake_post(monkeypatch, [httpx.Response(200, json={})])
    monkeypatch.setattr(client, "get_settings", lambda: settings(whatsapp_access_token=""))

    with pytest.raises(ConfigError):
        await client.send_image("92300", link=LINK)


async def test_send_text_still_works_through_the_shared_loop(monkeypatch: pytest.MonkeyPatch):
    calls = fake_post(monkeypatch, [httpx.Response(200, json={"messages": [{"id": "wamid.T"}]})])

    assert await client.send_text("92300", "hello") == "wamid.T"
    assert calls[0]["payload"]["type"] == "text"
    assert calls[0]["payload"]["text"]["body"] == "hello"
