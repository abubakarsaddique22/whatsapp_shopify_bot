"""A card number sent by a customer is never stored (Phase 11, Blueprint 8.8 and A10).

Each test works inside a transaction that is rolled back at the end, so your real rows stay.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.settings import get_settings
from app.db import repositories as repo
from app.db.tables import Message, WhatsAppInbox
from app.integrations.whatsapp.payloads import InboundMessage
from app.security.redaction import CARD_PLACEHOLDER

WA_ID = "920000000011"
CARD = "4111 1111 1111 1111"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            yield db
            await db.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


async def store_inbound(session: AsyncSession, text: str) -> str:
    wamid = f"wamid.test-{uuid.uuid4().hex[:12]}"
    msg = InboundMessage(wamid=wamid, wa_id=WA_ID, msg_type="text", text=text)
    conversation = await repo.get_or_create_conversation(session, WA_ID, "testhash")
    assert await repo.add_inbox(session, conversation, "testhash", msg) is not None
    return wamid


async def test_inbox_text_has_no_card_number(session: AsyncSession) -> None:
    wamid = await store_inbound(session, f"meri card {CARD} hai")
    stored = (
        await session.execute(select(WhatsAppInbox.text).where(WhatsAppInbox.wamid == wamid))
    ).scalar_one()
    assert stored == f"meri card {CARD_PLACEHOLDER} hai"
    assert "4111" not in stored


async def test_inbox_text_without_a_card_is_unchanged(session: AsyncSession) -> None:
    text = "03001234567 par call karein, watch Rs 12,500 ki hai?"
    wamid = await store_inbound(session, text)
    stored = (
        await session.execute(select(WhatsAppInbox.text).where(WhatsAppInbox.wamid == wamid))
    ).scalar_one()
    assert stored == text


async def test_message_transcript_has_no_card_number(session: AsyncSession) -> None:
    conversation = await repo.get_or_create_conversation(session, WA_ID, "testhash")
    wamid = f"wamid.test-{uuid.uuid4().hex[:12]}"
    await repo.add_message(session, conversation, "in", f"card number {CARD}", wamid)
    stored = (
        await session.execute(select(Message.body).where(Message.wamid == wamid))
    ).scalar_one()
    assert stored == f"card number {CARD_PLACEHOLDER}"


async def test_a_message_without_text_still_works(session: AsyncSession) -> None:
    wamid = f"wamid.test-{uuid.uuid4().hex[:12]}"
    msg = InboundMessage(wamid=wamid, wa_id=WA_ID, msg_type="image", media_id="m1")
    conversation = await repo.get_or_create_conversation(session, WA_ID, "testhash")
    assert await repo.add_inbox(session, conversation, "testhash", msg) is not None
    stored = (
        await session.execute(select(WhatsAppInbox.text).where(WhatsAppInbox.wamid == wamid))
    ).scalar_one()
    assert stored is None
