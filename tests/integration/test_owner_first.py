"""Bot ON / OFF button for the team, on the real Postgres with the real worker.

Bot OFF: a new message waits TEAM_WAIT_MINUTES; a team reply from the WhatsApp Business app
(smb_message_echoes) closes it silently; nobody answered -> the bot answers and turns itself ON.
Bot ON: the bot answers at once, also in a chat the team talked in before.
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
from app.owner.presence import BOT_AUTO_ON_FLAG, bot_is_off, record_owner_reply, set_bot
from tests.adversarial.harness import router_says
from tests.adversarial.worker_kit import Chat


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


async def bot_off(chat: Chat) -> Settings:
    settings = chat.use_settings(team_wait_minutes=3)
    await set_bot(chat.session, on=False)
    return settings


async def test_bot_off_a_new_message_waits_for_the_team(chat: Chat) -> None:
    await bot_off(chat)
    inbox_id = await chat.receive("Rolex ki price?")

    assert await worker.claim_next(chat.session) is None  # still the team's turn

    await chat.age(minutes=4)  # the wait is over
    inbox = await worker.claim_next(chat.session)
    assert inbox is not None and inbox.id == inbox_id


async def test_bot_on_takes_the_message_at_once(chat: Chat) -> None:
    chat.use_settings()
    inbox_id = await chat.receive("Rolex ki price?")
    inbox = await worker.claim_next(chat.session)
    assert inbox is not None and inbox.id == inbox_id


async def test_bot_off_and_the_team_answered_the_bot_stays_silent(chat: Chat) -> None:
    settings = await bot_off(chat)
    chat.use_model(router_says("price"))
    await chat.receive("Rolex ki price?")
    await record_owner_reply(chat.session, echo_to(chat), settings)  # the team answered
    await chat.age(minutes=4)

    inbox = await worker.claim_next(chat.session)
    assert inbox is not None
    outcome = await worker.handle_inbox(chat.session, inbox)

    assert await chat.status(inbox.id) == "ignored"
    assert await chat.bodies() == []  # no reply and no notice: a notice would cost money
    assert chat.llm.calls == []  # no model call
    assert outcome.alerts == []
    assert await bot_is_off(chat.session) is True  # the team is there: the bot stays OFF
    kept = await chat.kept_messages()
    assert "Rolex ki price?" in kept  # the customer's message is in the transcript
    assert "Ji, main dekhta hoon" in kept  # and so is the team's reply


async def test_bot_off_and_nobody_answered_the_bot_answers_and_turns_itself_on(
    chat: Chat,
) -> None:
    await bot_off(chat)
    inbox_id = await chat.receive("hello")
    await chat.age(minutes=4)

    inbox = await worker.claim_next(chat.session)
    assert inbox is not None and inbox.id == inbox_id
    outcome = await worker.handle_inbox(chat.session, inbox)

    assert await chat.status(inbox_id) == "done"
    assert len(await chat.bodies()) == 1  # the customer got an answer
    assert await bot_is_off(chat.session) is False  # the bot is ON again
    assert await repo.get_flag(chat.session, BOT_AUTO_ON_FLAG) is True
    assert [subject for subject, _ in outcome.alerts] == ["Bot khud ON ho gaya"]


async def test_a_team_reply_before_the_message_does_not_count(chat: Chat) -> None:
    settings = await bot_off(chat)
    await record_owner_reply(chat.session, echo_to(chat), settings)  # an earlier chat
    state = await chat.state()
    state.team_replied_at = (await repo.db_now(chat.session)).replace(year=2000)
    await repo.save_state(chat.session, await chat.conversation_id(), state)

    inbox_id = await chat.receive("aur koi colour?")
    await chat.age(minutes=4)
    inbox = await worker.claim_next(chat.session)
    assert inbox is not None
    await worker.handle_inbox(chat.session, inbox)

    assert await chat.status(inbox_id) == "done"  # nobody answered THIS message
    assert await bot_is_off(chat.session) is False


async def test_bot_on_answers_even_in_a_chat_the_team_talked_in_before(chat: Chat) -> None:
    settings = chat.use_settings()
    await record_owner_reply(chat.session, echo_to(chat), settings)
    state = await chat.state()
    state.team_replied_at = (await repo.db_now(chat.session)).replace(year=2000)
    await repo.save_state(chat.session, await chat.conversation_id(), state)

    inbox_id, _ = await chat.say("hello")

    assert await chat.status(inbox_id) == "done"
    assert len(await chat.bodies()) == 1


async def test_a_message_the_team_already_answered_is_never_answered_twice(chat: Chat) -> None:
    """Bot OFF, the team answers, then switches the bot ON before the wait is over."""
    settings = await bot_off(chat)
    inbox_id = await chat.receive("Rolex ki price?")
    await record_owner_reply(chat.session, echo_to(chat), settings)
    await set_bot(chat.session, on=True)

    inbox = await worker.claim_next(chat.session)  # bot ON: no waiting
    assert inbox is not None and inbox.id == inbox_id
    await worker.handle_inbox(chat.session, inbox)

    assert await chat.status(inbox_id) == "ignored"
    assert await chat.bodies() == []


async def test_an_echo_of_the_bots_own_reply_is_not_a_team_reply(chat: Chat) -> None:
    settings = chat.use_settings()
    conversation_id = await chat.conversation_id()
    ours = f"wamid.bot-{uuid.uuid4().hex[:12]}"
    chat.session.add(
        WhatsAppOutbox(conversation_id=conversation_id, seq=0, body="Hi", status="sent", wamid=ours)
    )
    await chat.session.flush()

    echo = OwnerEcho(wamid=ours, to_wa_id=chat.wa_id, msg_type="text", text="Hi")
    assert await record_owner_reply(chat.session, echo, settings) is None
    assert (await chat.state()).team_replied_at is None


async def test_a_team_message_to_a_new_customer_does_not_open_the_24_hour_window(
    chat: Chat,
) -> None:
    settings: Settings = chat.use_settings()
    conversation_id = await record_owner_reply(chat.session, echo_to(chat), settings)
    assert conversation_id is not None

    result = await chat.session.execute(
        select(Conversation.last_inbound_at).where(Conversation.id == conversation_id)
    )
    assert result.scalar_one() is None  # only a CUSTOMER message opens the window


async def test_switching_by_hand_clears_the_automatic_note(chat: Chat) -> None:
    await set_bot(chat.session, on=True, automatic=True)
    assert await repo.get_flag(chat.session, BOT_AUTO_ON_FLAG) is True
    await set_bot(chat.session, on=False)
    assert await repo.get_flag(chat.session, BOT_AUTO_ON_FLAG) is False
    assert await bot_is_off(chat.session) is True
