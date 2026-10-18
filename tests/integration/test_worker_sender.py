"""Worker (T25, kill switch) and sender (retry rules) tests on the real Postgres.

Each test works inside a transaction that is rolled back at the end. At the start the test
clears the inbox and outbox INSIDE that transaction (leftovers from manual testing would be
picked first), so after the rollback your real rows are still there.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import worker
from app.agent.templates import render
from app.core.errors import WhatsAppError, WhatsAppTemporaryError
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.db.tables import Message, WhatsAppInbox, WhatsAppOutbox
from app.integrations.llm.fake_llm import FakeLLM
from app.integrations.whatsapp import sender
from app.integrations.whatsapp.payloads import InboundMessage

WA_ID = "920000000009"


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


@pytest.fixture(autouse=True)
def _kill_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # debug_reply=False on purpose: a DEBUG_REPLY=true in .env must not reach the real LLM here
    monkeypatch.setattr(
        worker, "get_settings", lambda: Settings(kill_switch=False, debug_reply=False)
    )
    # the router now looks at every text: an empty FakeLLM makes it answer 'unclear' (no network)
    monkeypatch.setattr(worker, "get_chat_model", lambda role="router": FakeLLM([]))


# ---- helpers


async def conversation_id(session: AsyncSession) -> int:
    return await repo.get_or_create_conversation(session, WA_ID, "testhash")


async def queue_inbound(session: AsyncSession, msg_type: str = "text", text: str = "hello") -> int:
    msg = InboundMessage(
        wamid=f"wamid.test-{uuid.uuid4().hex[:12]}", wa_id=WA_ID, msg_type=msg_type, text=text
    )
    inbox_id = await repo.add_inbox(session, await conversation_id(session), "testhash", msg)
    assert inbox_id is not None
    return inbox_id


async def process(session: AsyncSession) -> None:
    """What the worker does for one message (without its own commit)."""
    inbox = await repo.claim_inbox(session)
    assert inbox is not None
    await worker.handle_inbox(session, inbox)


async def queue_reply(session: AsyncSession) -> None:
    inbox_id = await queue_inbound(session)
    await repo.add_outbox(session, await conversation_id(session), inbox_id, 0, "Received")


async def inbox_status(session: AsyncSession, inbox_id: int) -> str:
    result = await session.execute(select(WhatsAppInbox.status).where(WhatsAppInbox.id == inbox_id))
    return result.scalar_one()


async def outbox_rows(session: AsyncSession) -> list[Any]:
    result = await session.execute(
        select(
            WhatsAppOutbox.body,
            WhatsAppOutbox.status,
            WhatsAppOutbox.attempts,
            WhatsAppOutbox.last_error,
            WhatsAppOutbox.wamid,
        ).order_by(WhatsAppOutbox.id)
    )
    return list(result.all())


def fake_send(monkeypatch: pytest.MonkeyPatch, result: str | Exception) -> None:
    """Replace the real WhatsApp call: return a wamid, or raise the given error."""

    async def fake(to: str, body: str) -> str:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(sender, "send_text", fake)


# ---- worker


async def test_text_message_goes_through_the_graph(session: AsyncSession) -> None:
    """The router (empty FakeLLM) answers 'unclear', so the graph asks a clarifying question."""
    inbox_id = await queue_inbound(session, "text", "hello")
    await process(session)

    assert [r.body for r in await outbox_rows(session)] == [render("ask_detail", "roman_ur")]
    assert await inbox_status(session, inbox_id) == "done"
    result = await session.execute(
        select(Message.direction, Message.body).where(
            Message.conversation_id == await conversation_id(session)
        )
    )
    assert [tuple(r) for r in result.all()] == [("in", "hello")]


@pytest.mark.parametrize("msg_type", ["image", "audio", "other"])
async def test_t25_unsupported_type_gets_a_polite_reply(
    session: AsyncSession, msg_type: str
) -> None:
    inbox_id = await queue_inbound(session, msg_type, "")
    await process(session)

    assert [r.body for r in await outbox_rows(session)] == [worker.UNSUPPORTED_REPLY]
    assert await inbox_status(session, inbox_id) == "done"


async def test_processing_the_same_message_twice_queues_one_reply(session: AsyncSession) -> None:
    """A retry after a crash must not send the customer two replies."""
    await queue_inbound(session)
    inbox = await repo.claim_inbox(session)
    assert inbox is not None
    await worker.handle_inbox(session, inbox)
    await worker.handle_inbox(session, inbox)  # the same row again, like a retry
    assert len(await outbox_rows(session)) == 1


async def test_kill_switch_ignores_the_message_and_queues_no_reply(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        worker, "get_settings", lambda: Settings(kill_switch=True, debug_reply=False)
    )
    inbox_id = await queue_inbound(session)
    await process(session)

    assert await inbox_status(session, inbox_id) == "ignored"
    assert await outbox_rows(session) == []


async def test_inbox_failure_retries_then_gives_up(session: AsyncSession) -> None:
    inbox_id = await queue_inbound(session)
    for attempt in range(1, worker.MAX_ATTEMPTS + 1):
        await repo.record_inbox_failure(session, inbox_id, "boom", worker.MAX_ATTEMPTS)
        expected = "failed" if attempt == worker.MAX_ATTEMPTS else "pending"
        assert await inbox_status(session, inbox_id) == expected


# ---- sender


async def test_nothing_to_send_returns_false(session: AsyncSession) -> None:
    assert await sender.send_one(session) is False


async def test_successful_send_marks_sent_and_saves_the_wamid(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await queue_reply(session)
    fake_send(monkeypatch, "wamid.test-out")

    assert await sender.send_one(session) is True

    row = (await outbox_rows(session))[0]
    assert (row.status, row.wamid, row.attempts) == ("sent", "wamid.test-out", 1)
    result = await session.execute(
        select(Message.direction, Message.body).where(Message.wamid == "wamid.test-out")
    )
    assert tuple(result.one()) == ("out", "Received")
    assert await sender.send_one(session) is False  # a sent row is never sent again


async def test_temporary_error_keeps_the_row_pending(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await queue_reply(session)
    fake_send(monkeypatch, WhatsAppTemporaryError("HTTP 500", detail={"meta_code": None}))

    assert await sender.send_one(session) is True

    row = (await outbox_rows(session))[0]
    assert (row.status, row.attempts) == ("pending", 1)
    assert "HTTP 500" in row.last_error


async def test_temporary_errors_end_as_failed_after_max_attempts(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await queue_reply(session)
    fake_send(monkeypatch, WhatsAppTemporaryError("HTTP 500"))

    for _ in range(sender.MAX_ATTEMPTS):
        assert await sender.send_one(session) is True

    row = (await outbox_rows(session))[0]
    assert (row.status, row.attempts) == ("failed", sender.MAX_ATTEMPTS)
    assert await sender.send_one(session) is False  # a dead letter is not tried again


async def test_permanent_error_fails_at_once_and_is_not_retried(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await queue_reply(session)
    fake_send(monkeypatch, WhatsAppError("HTTP 400", detail={"meta_code": 131031}))

    assert await sender.send_one(session) is True

    row = (await outbox_rows(session))[0]
    assert (row.status, row.attempts) == ("failed", 1)
    assert "131031" in row.last_error
    assert await sender.send_one(session) is False
