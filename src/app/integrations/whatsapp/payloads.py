"""Parse Meta WhatsApp webhook payloads and build outbound message payloads.

Meta's JSON is nested: entry -> changes -> value -> messages / statuses.
We validate it with small private models (extra='ignore', so new Meta fields never break us)
and flatten it into InboundMessage and StatusUpdate.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)


# ---- What we hand to the rest of the app


class InboundMessage(BaseModel):
    wamid: str
    wa_id: str  # sender phone number, as Meta sends it
    msg_type: str  # text | audio | image | other
    text: str | None = None  # text body, or the caption of an image
    media_id: str | None = None  # used by Phase 6 (audio) and Phase 7 (images)
    mime_type: str | None = None
    wa_timestamp: datetime | None = None


class StatusUpdate(BaseModel):
    wamid: str  # the wamid of OUR outbound message
    status: str  # sent | delivered | read | failed
    recipient_id: str = ""
    error_code: int | None = None


class ParsedWebhook(BaseModel):
    messages: list[InboundMessage] = []
    statuses: list[StatusUpdate] = []


# ---- Raw Meta shapes (private)


class _Raw(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _RawText(_Raw):
    body: str = ""


class _RawMedia(_Raw):
    id: str
    mime_type: str | None = None
    caption: str | None = None


class _RawMessage(_Raw):
    id: str
    from_: str = Field(alias="from")
    type: str = ""
    timestamp: str | None = None
    text: _RawText | None = None
    audio: _RawMedia | None = None
    image: _RawMedia | None = None


class _RawError(_Raw):
    code: int | None = None


class _RawStatus(_Raw):
    id: str
    status: str
    recipient_id: str = ""
    errors: list[_RawError] = []


class _RawValue(_Raw):
    messages: list[_RawMessage] = []
    statuses: list[_RawStatus] = []


class _RawChange(_Raw):
    value: _RawValue = Field(default_factory=_RawValue)


class _RawEntry(_Raw):
    changes: list[_RawChange] = []


class _RawPayload(_Raw):
    entry: list[_RawEntry] = []


# ---- Parsing


def _to_datetime(ts: str | None) -> datetime | None:
    """Meta sends the time as epoch seconds in a string."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _to_inbound(raw: _RawMessage) -> InboundMessage:
    msg = InboundMessage(
        wamid=raw.id,
        wa_id=raw.from_,
        msg_type="other",
        wa_timestamp=_to_datetime(raw.timestamp),
    )
    if raw.type == "text" and raw.text is not None:
        msg.msg_type = "text"
        msg.text = raw.text.body
    elif raw.type == "audio" and raw.audio is not None:
        msg.msg_type = "audio"
        msg.media_id = raw.audio.id
        msg.mime_type = raw.audio.mime_type
    elif raw.type == "image" and raw.image is not None:
        msg.msg_type = "image"
        msg.media_id = raw.image.id
        msg.mime_type = raw.image.mime_type
        msg.text = raw.image.caption
    # anything else (sticker, location, video, button...) stays "other"
    return msg


def parse_webhook(payload: dict[str, Any]) -> ParsedWebhook:
    """Flatten a Meta webhook body. A payload we cannot read gives an empty result, not an error,
    so the webhook can still answer 200 and Meta does not keep retrying it."""
    try:
        raw = _RawPayload.model_validate(payload)
    except ValidationError:
        log.warning("whatsapp payload could not be parsed")
        return ParsedWebhook()

    result = ParsedWebhook()
    for entry in raw.entry:
        for change in entry.changes:
            for m in change.value.messages:
                result.messages.append(_to_inbound(m))
            for s in change.value.statuses:
                code = s.errors[0].code if s.errors else None
                result.statuses.append(
                    StatusUpdate(
                        wamid=s.id,
                        status=s.status,
                        recipient_id=s.recipient_id,
                        error_code=code,
                    )
                )
    return result


# ---- Outbound


def build_image_payload(
    to: str, *, link: str | None = None, media_id: str | None = None, caption: str | None = None
) -> dict[str, Any]:
    """Body for sending a picture: either a public `link` (Meta fetches it) or an uploaded
    `media_id`. Exactly one of them. The caption is left out when empty."""
    if (link is None) == (media_id is None):
        raise ValueError("give exactly one of link or media_id")
    image: dict[str, str] = {"link": link} if link is not None else {"id": media_id or ""}
    if caption:
        image["caption"] = caption
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "image",
        "image": image,
    }


def build_text_payload(to: str, body: str) -> dict[str, Any]:
    """Body for POST /{phone_number_id}/messages."""
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
