"""P8 owner first, bot as backup, on the real Postgres with the real worker.

In owner hours a new message waits OWNER_WAIT_MINUTES; an owner reply from the WhatsApp Business
app (smb_message_echoes) makes the bot silent for that customer, with no notice and no model call.
Every test is rolled back. Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import worker
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.db.tables import Conversation, Handoff, WhatsAppInbox, WhatsAppOutbox
from app.integrations.whatsapp.payloads import OwnerEcho
from app.owner.presence import OWNER_AWAY_FLAG, record_owner_reply
from tests.adversarial.harness import router_says
from tests.adversarial.worker_kit import Chat

OWNER_ON = {"owner_first": True, "owner_wait_minutes": 3}  # Chat's hours: 00:00-00:00 = all day


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            session = AsyncSession(bind=conn, expire_on_commit=False)
            # the worker takes the oldest pending row of ANY customer: start from an empty queue
            await session.execute(delete(WhatsAppOutbox))
            await session.execute(delete(WhatsAppInbox))
            await session.execute(delete(Handoff))
            yield session
            await session.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def chat(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Chat:
    return Chat(db, monkeypatch, wa_id="920000000088", wa_hash="testhash-own1")


def echo_to(chat: Chat, text: str = "Ji, main dekhta hoon") -> OwnerEcho:
    return OwnerEcho(
        wamid=f"wamid.echo-{uuid.uuid4().hex[:12]}", to_wa_id=chat.wa_id, msg_type="text", text=text
    )


async def test_a_new_message_waits_for_the_owner_in_owner_hours(chat: Chat) -> None:
    chat.use_settings(**OWNER_ON)
    inbox_id = await chat.receive("Rolex ki price?")

    assert await worker.claim_next(chat.session) is None  # still the owner's turn

    await chat.age(minutes=4)  # the wait is over and the owner did not answer
    inbox = await worker.claim_next(chat.session)
    assert inbox is not None and inbox.id == inbox_id


async def test_with_owner_first_off_the_bot_takes_the_message_at_once(chat: Chat) -> None:
    chat.use_settings(owner_first=False)
    inbox_id = await chat.receive("Rolex ki price?")
    inbox = await worker.claim_next(chat.session)
    assert inbox is not None and inbox.id == inbox_id


async def test_owner_away_means_the_bot_answers_at_once(chat: Chat) -> None:
    chat.use_settings(**OWNER_ON)
    await repo.set_flag(chat.session, OWNER_AWAY_FLAG, True)
    inbox_id = await chat.receive("Rolex ki price?")
    inbox = await worker.claim_next(chat.session)
    assert inbox is not None and inbox.id == inbox_id


async def test_outside_owner_hours_the_bot_answers_at_once(chat: Chat) -> None:
    now = await repo.db_now(chat.session)
    # a one-minute owner window that is surely not now (in UTC+00:00)
    other = (now.hour + 12) % 24
    chat.use_settings(
        **OWNER_ON, owner_hours=f"{other:02d}:00-{other:02d}:01", business_utc_offset="+00:00"
    )
    inbox_id = await chat.receive("Rolex ki price?")
    inbox = await worker.claim_next(chat.session)
    assert inbox is not None and inbox.id == inbox_id


async def test_after_an_owner_reply_the_bot_stays_silent_and_costs_nothing(chat: Chat) -> None:
    settings = chat.use_settings(**OWNER_ON)
    chat.use_model(router_says("price"))
    await chat.receive("Rolex ki price?")
    await record_owner_reply(chat.session, echo_to(chat), settings)  # owner answered in the app
    await chat.age(minutes=4)

    inbox = await worker.claim_next(chat.session)
    assert inbox is not None
    await worker.handle_inbox(chat.session, inbox)

    assert await chat.status(inbox.id) == "ignored"
    assert await chat.bodies() == []  # no reply and no notice: a notice would cost money
    assert chat.llm.calls == []  # no model call
    kept = await chat.kept_messages()
    assert "Rolex ki price?" in kept  # the customer's message is in the transcript
    assert "Ji, main dekhta hoon" in kept  # and so is the owner's reply


async def test_the_owner_pause_works_even_with_owner_first_off(chat: Chat) -> None:
    settings = chat.use_settings(owner_first=False)
    await record_owner_reply(chat.session, echo_to(chat), settings)

    inbox_id, _ = await chat.say("aur koi colour?")

    assert await chat.status(inbox_id) == "ignored"
    assert await chat.bodies() == []


async def test_when_the_owner_pause_ends_the_bot_answers_again(chat: Chat) -> None:
    settings = chat.use_settings(owner_first=False)
    await record_owner_reply(chat.session, echo_to(chat), settings)
    state = await chat.state()
    assert state.owner_active_until is not None
    state.owner_active_until = (await repo.db_now(chat.session)).replace(year=2000)
    await repo.save_state(chat.session, await chat.conversation_id(), state)

    inbox_id, _ = await chat.say("hello")

    assert await chat.status(inbox_id) == "done"
    assert len(await chat.bodies()) == 1


async def test_an_echo_of_the_bots_own_reply_does_not_silence_the_bot(chat: Chat) -> None:
    settings = chat.use_settings(owner_first=False)
    conversation_id = await chat.conversation_id()
    ours = f"wamid.bot-{uuid.uuid4().hex[:12]}"
    chat.session.add(
        WhatsAppOutbox(conversation_id=conversation_id, seq=0, body="Hi", status="sent", wamid=ours)
    )
    await chat.session.flush()

    echo = OwnerEcho(wamid=ours, to_wa_id=chat.wa_id, msg_type="text", text="Hi")
    assert await record_owner_reply(chat.session, echo, settings) is None
    assert (await chat.state()).owner_active_until is None


async def test_an_owner_message_to_a_new_customer_does_not_open_the_24_hour_window(
    chat: Chat,
) -> None:
    settings: Settings = chat.use_settings(owner_first=False)
    conversation_id = await record_owner_reply(chat.session, echo_to(chat), settings)
    assert conversation_id is not None

    result = await chat.session.execute(
        select(Conversation.last_inbound_at).where(Conversation.id == conversation_id)
    )
    assert result.scalar_one() is None  # only a CUSTOMER message opens the window
