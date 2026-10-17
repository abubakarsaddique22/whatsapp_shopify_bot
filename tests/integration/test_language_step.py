"""Phase 5, step 6: the language step on the real Postgres, with a FakeLLM (no network).

Checks that the sticky language is saved and read back, that two messages in a row switch it,
and that the worker's debug reply (DEBUG_REPLY=true) shows what the router understood.
Each test runs inside a transaction that is rolled back, so your real rows stay as they are.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import worker
from app.agent.language_step import run_language_step
from app.agent.templates import render
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.db.tables import WhatsAppInbox, WhatsAppOutbox
from app.domain.models import Intent, Lang
from app.integrations.llm.fake_llm import FakeLLM
from app.integrations.whatsapp.payloads import InboundMessage

WA_ID = "920000000008"


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


def answer(language: str = "en", intent: str = "price", **fields: Any) -> str:
    data = {"language": language, "intent": intent, "confidence": 0.9, **fields}
    return json.dumps(data)


async def new_conversation(session: AsyncSession) -> int:
    return await repo.get_or_create_conversation(session, WA_ID, "testhash")


# ---- state storage


async def test_new_conversation_has_a_fresh_state(session: AsyncSession) -> None:
    cid = await new_conversation(session)
    state = await repo.load_state(session, cid)
    assert state.language == Lang.UNKNOWN
    assert state.last_router_output is None
    assert state.sender_hash == "testhash"
    assert state.conversation_id == str(cid)


async def test_saved_state_is_read_back_and_lang_column_follows(session: AsyncSession) -> None:
    cid = await new_conversation(session)
    llm = FakeLLM([answer("roman_ur", "availability")])
    await run_language_step(session, cid, "kya yeh available hai?", llm)

    state = await repo.load_state(session, cid)
    assert state.language == Lang.ROMAN_UR
    assert state.last_router_output is not None
    assert state.last_router_output.intent == Intent.AVAILABILITY

    from app.db.tables import Conversation

    lang = (
        await session.execute(select(Conversation.lang).where(Conversation.id == cid))
    ).scalar_one()
    assert lang == "roman_ur"


async def test_load_state_of_a_missing_conversation_fails(session: AsyncSession) -> None:
    with pytest.raises(LookupError):
        await repo.load_state(session, 987654321)


# ---- the language step


async def test_first_message_sets_the_language_and_reply_language(session: AsyncSession) -> None:
    cid = await new_conversation(session)
    result = await run_language_step(session, cid, "How much is it?", FakeLLM([answer("en")]))
    assert result.conversation_language == Lang.EN
    assert result.reply_language == "en"


async def test_one_stray_message_does_not_switch_but_two_in_a_row_do(
    session: AsyncSession,
) -> None:
    cid = await new_conversation(session)
    llm = FakeLLM([answer("en"), answer("roman_ur"), answer("roman_ur")])

    first = await run_language_step(session, cid, "How much is it?", llm)
    second = await run_language_step(session, cid, "iski price kya hai", llm)
    third = await run_language_step(session, cid, "aur colour kon sa hai", llm)

    assert first.conversation_language == Lang.EN
    assert second.conversation_language == Lang.EN  # one message is not enough
    assert third.conversation_language == Lang.ROMAN_UR  # the second one in a row switches
    assert third.reply_language == "roman_ur"


async def test_explicit_request_switches_at_once(session: AsyncSession) -> None:
    cid = await new_conversation(session)
    llm = FakeLLM([answer("roman_ur"), answer("roman_ur")])
    await run_language_step(session, cid, "iski price kya hai", llm)
    result = await run_language_step(session, cid, "English mein bataein", llm)
    assert result.conversation_language == Lang.EN
    assert result.reply_language == "en"


async def test_router_failure_keeps_the_conversation_working(session: AsyncSession) -> None:
    """Two bad answers: the router falls back to 'unclear' and the language stays unknown."""
    cid = await new_conversation(session)
    result = await run_language_step(session, cid, "???", FakeLLM(["nope", "still nope"]))
    assert result.router.intent == Intent.UNCLEAR
    assert result.reply_language == "en_roman_ur"


# ---- worker debug reply


async def test_debug_reply_shows_what_the_router_understood(session: AsyncSession) -> None:
    cid = await new_conversation(session)
    llm = FakeLLM([answer("roman_ur", "price", product_query="G-Shock GA-2100")])
    text = await worker.debug_language_reply(session, cid, "GA-2100 ki price kya hai", llm)
    assert text.startswith("[debug] language=roman_ur | intent=price")
    assert "reply_in=roman_ur" in text
    assert "product=G-Shock GA-2100" in text


async def test_worker_sends_the_debug_reply_when_switched_on(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        worker, "get_settings", lambda: Settings(kill_switch=False, debug_reply=True)
    )
    llm = FakeLLM([answer("en", "greeting")])
    monkeypatch.setattr(worker, "get_chat_model", lambda role="router": llm)

    cid = await new_conversation(session)
    msg = InboundMessage(
        wamid=f"wamid.test-{uuid.uuid4().hex[:12]}", wa_id=WA_ID, msg_type="text", text="hello"
    )
    assert await repo.add_inbox(session, cid, "testhash", msg) is not None

    inbox = await repo.claim_inbox(session)
    assert inbox is not None
    await worker.handle_inbox(session, inbox)

    bodies = (await session.execute(select(WhatsAppOutbox.body))).scalars().all()
    assert len(bodies) == 1
    assert bodies[0].startswith("[debug] language=en | intent=greeting")


async def test_worker_without_the_switch_uses_the_graph(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        worker, "get_settings", lambda: Settings(kill_switch=False, debug_reply=False)
    )
    monkeypatch.setattr(worker, "get_chat_model", lambda role="router": FakeLLM([]))  # no network
    cid = await new_conversation(session)
    msg = InboundMessage(
        wamid=f"wamid.test-{uuid.uuid4().hex[:12]}", wa_id=WA_ID, msg_type="text", text="hello"
    )
    await repo.add_inbox(session, cid, "testhash", msg)
    inbox = await repo.claim_inbox(session)
    assert inbox is not None
    await worker.handle_inbox(session, inbox)

    bodies = (await session.execute(select(WhatsAppOutbox.body))).scalars().all()
    # the empty FakeLLM router answers 'unclear': the graph asks a clarifying question
    assert list(bodies) == [render("ask_detail", "en_roman_ur")]
