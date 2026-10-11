"""Unit tests for integrations/whatsapp/payloads.py (pure functions, no network, no database)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.integrations.whatsapp.payloads import build_text_payload, parse_webhook

FIXTURE = Path(__file__).parent / "text_message.json"


def _payload(
    messages: list[dict[str, Any]] | None = None,
    statuses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {"messaging_product": "whatsapp"}
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": value}]}]}


def _msg(msg_type: str, **extra: Any) -> dict[str, Any]:
    return {
        "from": "923001234567",
        "id": f"wamid.{msg_type}",
        "timestamp": "1760169600",
        "type": msg_type,
        **extra,
    }


def test_real_text_fixture() -> None:
    parsed = parse_webhook(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert parsed.statuses == []
    assert len(parsed.messages) == 1
    m = parsed.messages[0]
    assert m.wamid == "wamid.HBgLOTIzMDAxMjM0NTY3FQIAEhgg"
    assert m.wa_id == "923001234567"
    assert m.msg_type == "text"
    assert m.text == "hello"
    assert m.wa_timestamp == datetime.fromtimestamp(1760169600, tz=UTC)


def test_audio_message() -> None:
    audio = {"id": "media-1", "mime_type": "audio/ogg; codecs=opus", "voice": True}
    m = parse_webhook(_payload([_msg("audio", audio=audio)])).messages[0]
    assert m.msg_type == "audio"
    assert m.media_id == "media-1"
    assert m.mime_type == "audio/ogg; codecs=opus"
    assert m.text is None


def test_image_message_keeps_caption() -> None:
    image = {"id": "media-2", "mime_type": "image/jpeg", "caption": "ye wali chahiye"}
    m = parse_webhook(_payload([_msg("image", image=image)])).messages[0]
    assert m.msg_type == "image"
    assert m.media_id == "media-2"
    assert m.text == "ye wali chahiye"


def test_unsupported_type_is_other() -> None:
    sticker = _msg("sticker", sticker={"id": "media-3", "mime_type": "image/webp"})
    m = parse_webhook(_payload([sticker])).messages[0]
    assert m.msg_type == "other"
    assert m.text is None
    assert m.media_id is None


def test_multi_message_payload_keeps_order() -> None:
    first = _msg("text", text={"body": "one"})
    second = {**_msg("text", text={"body": "two"}), "id": "wamid.second"}
    parsed = parse_webhook(_payload([first, second]))
    assert [m.text for m in parsed.messages] == ["one", "two"]
    assert [m.wamid for m in parsed.messages] == ["wamid.text", "wamid.second"]


def test_status_only_payload() -> None:
    status = {"id": "wamid.out1", "status": "delivered", "recipient_id": "923001234567"}
    parsed = parse_webhook(_payload(statuses=[status]))
    assert parsed.messages == []
    assert len(parsed.statuses) == 1
    assert parsed.statuses[0].wamid == "wamid.out1"
    assert parsed.statuses[0].status == "delivered"
    assert parsed.statuses[0].error_code is None


def test_failed_status_carries_error_code() -> None:
    status = {
        "id": "wamid.out2",
        "status": "failed",
        "recipient_id": "923001234567",
        "errors": [{"code": 131031, "title": "Business Account locked"}],
    }
    parsed = parse_webhook(_payload(statuses=[status]))
    assert parsed.statuses[0].status == "failed"
    assert parsed.statuses[0].error_code == 131031


def test_unreadable_payload_gives_empty_result() -> None:
    assert parse_webhook({"entry": "not a list"}).messages == []
    assert parse_webhook({}).messages == []
    # a message without an id is invalid; we skip the whole payload instead of crashing
    bad = _payload([{"from": "923001234567", "type": "text"}])
    parsed = parse_webhook(bad)
    assert parsed.messages == []
    assert parsed.statuses == []


def test_bad_timestamp_becomes_none() -> None:
    msg = {**_msg("text", text={"body": "hi"}), "timestamp": "not-a-number"}
    assert parse_webhook(_payload([msg])).messages[0].wa_timestamp is None


def test_build_text_payload() -> None:
    assert build_text_payload("923001234567", "Received") == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "923001234567",
        "type": "text",
        "text": {"preview_url": False, "body": "Received"},
    }
