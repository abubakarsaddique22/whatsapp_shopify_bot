"""The fake voice note that /dev/simulate builds (Phase 6, step 4c). No server, no database.

It must look like what Meta sends, so the REAL parser turns it into an audio message whose
media id starts with "dev-" (that prefix is what makes the worker skip the download)."""

from app.api.dev import SimulateRequest, _payload
from app.integrations.whatsapp.payloads import parse_webhook


def test_the_simulated_voice_note_is_parsed_as_audio() -> None:
    body = SimulateRequest(msg_type="audio")
    parsed = parse_webhook(_payload(body, "wamid.dev-1"))

    (message,) = parsed.messages
    assert message.msg_type == "audio"
    assert message.media_id is not None
    assert message.media_id.startswith("dev-")
    assert message.mime_type == "audio/ogg; codecs=opus"
    assert message.text is None


def test_text_and_image_are_unchanged() -> None:
    text = parse_webhook(_payload(SimulateRequest(text="hi"), "wamid.dev-2")).messages[0]
    image = parse_webhook(_payload(SimulateRequest(msg_type="image"), "wamid.dev-3")).messages[0]
    assert (text.msg_type, text.text) == ("text", "hi")
    assert (image.msg_type, image.media_id) == ("image", "dev-media")
