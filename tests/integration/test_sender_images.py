"""Sending pictures from the outbox (Phase 7, step 3; T24 in the sender part).

Same method as test_worker_sender.py: a transaction on the real Postgres that is rolled back, the
inbox and outbox cleared INSIDE it, and the WhatsApp calls replaced by fakes.
Needs: docker compose up -d db, and uv run alembic upgrade head (the c5e1b8a47d92 migration).
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.catalog.image_formats import ImageConversionError, jpeg_rendition_url
from app.core.errors import WhatsAppError, WhatsAppTemporaryError
from app.core.settings import get_settings
from app.db import repositories as repo
from app.db.tables import Message, WhatsAppInbox, WhatsAppOutbox
from app.integrations.whatsapp import sender
from app.integrations.whatsapp.payloads import InboundMessage

WA_ID = "920000000010"
URL = "https://cdn.shopify.com/s/files/1/1/files/reef.webp?v=1"
FALLBACK = "Vantor Reef: https://shop.example/products/vantor-reef"
REFUSED = WhatsAppError("HTTP 400", detail={"meta_code": 131053})


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            await db.execute(delete(WhatsAppOutbox))
            await db.execute(delete(WhatsAppInbox))
            yield db
            await db.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


# ---- helpers


async def queue_image(session: AsyncSession) -> None:
    conversation = await repo.get_or_create_conversation(session, WA_ID, "testhash")
    message = InboundMessage(
        wamid=f"wamid.test-{uuid.uuid4().hex[:12]}", wa_id=WA_ID, msg_type="text", text="picture"
    )
    inbox_id = await repo.add_inbox(session, conversation, "testhash", message)
    assert inbox_id is not None
    await repo.add_outbox(
        session, conversation, inbox_id, 0, FALLBACK, kind="image", media_url=URL, caption="Reef"
    )


async def row(session: AsyncSession) -> Any:
    result = await session.execute(
        select(
            WhatsAppOutbox.kind,
            WhatsAppOutbox.status,
            WhatsAppOutbox.attempts,
            WhatsAppOutbox.last_error,
            WhatsAppOutbox.wamid,
            WhatsAppOutbox.media_url,
            WhatsAppOutbox.body,
        )
    )
    return result.one()


def fake_send_image(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[str | Exception]
) -> list[dict[str, Any]]:
    """Each call returns (or raises) the next outcome. Returns the recorded calls."""
    calls: list[dict[str, Any]] = []

    async def fake(
        to: str, *, link: str | None = None, media_id: str | None = None, caption: str | None = None
    ) -> str:
        calls.append({"link": link, "media_id": media_id, "caption": caption})
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sender, "send_image", fake)
    return calls


def fake_fetch(monkeypatch: pytest.MonkeyPatch, result: tuple[bytes, str] | Exception) -> list[str]:
    calls: list[str] = []

    async def fake(url: str, **_: Any) -> tuple[bytes, str]:
        calls.append(url)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(sender, "fetch_and_prepare", fake)
    return calls


def fake_upload(monkeypatch: pytest.MonkeyPatch, media_id: str = "MEDIA1") -> None:
    async def fake(data: bytes, mime: str, **_: Any) -> str:
        return media_id

    monkeypatch.setattr(sender, "upload_image", fake)


def fake_send_text(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    bodies: list[str] = []

    async def fake(to: str, body: str) -> str:
        bodies.append(body)
        return "wamid.text-out"

    monkeypatch.setattr(sender, "send_text", fake)
    return bodies


# ---- the picture goes out


async def test_picture_is_sent_by_jpeg_link(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await queue_image(session)
    calls = fake_send_image(monkeypatch, ["wamid.img"])

    assert await sender.send_one(session) is True

    assert calls == [{"link": jpeg_rendition_url(URL), "media_id": None, "caption": "Reef"}]
    assert "format=jpg" in calls[0]["link"]
    sent = await row(session)
    assert (sent.kind, sent.status, sent.wamid, sent.attempts) == ("image", "sent", "wamid.img", 1)
    transcript = await session.execute(
        select(Message.body).where(Message.wamid == "wamid.img")
    )
    assert transcript.scalar_one() == f"[image] {FALLBACK}"
    assert await sender.send_one(session) is False  # a sent row is never sent again


async def test_refused_link_is_retried_once_by_upload(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await queue_image(session)
    calls = fake_send_image(monkeypatch, [REFUSED, "wamid.up"])
    fetched = fake_fetch(monkeypatch, (b"jpeg-bytes", "image/jpeg"))
    fake_upload(monkeypatch, "MEDIA1")

    await sender.send_one(session)

    assert fetched == [URL]
    assert calls[1] == {"link": None, "media_id": "MEDIA1", "caption": "Reef"}
    sent = await row(session)
    assert (sent.kind, sent.status, sent.wamid) == ("image", "sent", "wamid.up")


# ---- T24: the picture cannot be delivered


async def test_t24_failed_picture_becomes_a_text_link(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await queue_image(session)
    fake_send_image(monkeypatch, [REFUSED])
    fake_fetch(monkeypatch, WhatsAppError("Product picture not available (HTTP 404)"))
    bodies = fake_send_text(monkeypatch)

    await sender.send_one(session)  # the picture fails, the row turns into text

    changed = await row(session)
    assert (changed.kind, changed.status, changed.attempts) == ("text", "pending", 0)
    assert changed.media_url is None
    assert "image fallback" in changed.last_error

    await sender.send_one(session)  # the text fallback goes out

    assert bodies == [FALLBACK]
    done = await row(session)
    assert (done.kind, done.status, done.wamid) == ("text", "sent", "wamid.text-out")


async def test_t24_temporary_error_is_retried_once_then_text_link(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await queue_image(session)
    calls = fake_send_image(
        monkeypatch, [WhatsAppTemporaryError("HTTP 500"), WhatsAppTemporaryError("HTTP 500")]
    )
    fake_fetch(monkeypatch, AssertionError("a temporary error must not start the upload path"))
    bodies = fake_send_text(monkeypatch)

    await sender.send_one(session)
    first = await row(session)
    assert (first.kind, first.status, first.attempts) == ("image", "pending", 1)

    await sender.send_one(session)
    second = await row(session)
    assert (second.kind, second.status, second.attempts) == ("text", "pending", 0)

    await sender.send_one(session)
    assert len(calls) == 2
    assert bodies == [FALLBACK]
    assert (await row(session)).status == "sent"


async def test_t24_picture_that_cannot_be_converted_becomes_a_text_link(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await queue_image(session)
    fake_send_image(monkeypatch, [REFUSED])
    fake_fetch(monkeypatch, ImageConversionError("Could not read the picture"))
    bodies = fake_send_text(monkeypatch)

    await sender.send_one(session)
    await sender.send_one(session)

    assert bodies == [FALLBACK]
    assert (await row(session)).status == "sent"
