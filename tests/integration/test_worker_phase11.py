"""Phase 11, step 7: the worker wiring (rate limit, pause, daily budget, card warning, handoff).

Real Postgres; every test is rolled back (same idea as test_worker_sender.py). The model is an
empty FakeLLM: the router answers 'unclear', so a normal message gets the `ask_detail` question
and the third one in a row becomes a handoff.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import worker
from app.agent.templates import render
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.db.tables import Handoff, Message, WhatsAppInbox, WhatsAppOutbox
from app.handoff import service as handoff_service
from app.integrations.llm.fake_llm import FakeLLM
from app.integrations.whatsapp.payloads import InboundMessage

WA_ID = "920000000011"
HASH = "testhash11"
LANGS = ("en", "roman_ur", "en_roman_ur")


def any_lang(kind: str) -> set[str]:
    """The text of one template in every reply language (the test does not care which)."""
    return {render(kind, lang) for lang in LANGS}  # type: ignore[arg-type]


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
def _fake_model_and_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch)
    monkeypatch.setattr(worker, "get_chat_model", lambda role="router": FakeLLM([]))


def use_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Settings for the worker. 00:00-00:00 = the team is open all day (no after-hours note)."""
    values: dict[str, Any] = {"debug_reply": False, "business_hours": "00:00-00:00", **overrides}
    monkeypatch.setattr(worker, "get_settings", lambda: Settings(**values))


# ---- helpers


async def conversation_id(session: AsyncSession) -> int:
    return await repo.get_or_create_conversation(session, WA_ID, HASH)


async def receive(session: AsyncSession, text: str = "hmm") -> int:
    msg = InboundMessage(
        wamid=f"wamid.test-{uuid.uuid4().hex[:12]}", wa_id=WA_ID, msg_type="text", text=text
    )
    inbox_id = await repo.add_inbox(session, await conversation_id(session), HASH, msg)
    assert inbox_id is not None
    return inbox_id


async def process(session: AsyncSession) -> worker.TurnOutcome:
    inbox = await repo.claim_inbox(session)
    assert inbox is not None
    return await worker.handle_inbox(session, inbox)


async def inbox_status(session: AsyncSession, inbox_id: int) -> str:
    result = await session.execute(select(WhatsAppInbox.status).where(WhatsAppInbox.id == inbox_id))
    return result.scalar_one()


async def bodies(session: AsyncSession) -> list[str]:
    result = await session.execute(select(WhatsAppOutbox.body).order_by(WhatsAppOutbox.id))
    return [row[0] for row in result.all()]


async def handoffs(session: AsyncSession) -> list[Any]:
    result = await session.execute(
        select(Handoff.reason, Handoff.status).where(
            Handoff.conversation_id == await conversation_id(session)
        )
    )
    return list(result.all())


# ---- rate limit


async def test_over_the_limit_one_polite_message_then_silence(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch, rate_limit_per_minute=2)
    for _ in range(2):  # inside the limit: normal answers
        await receive(session)
        await process(session)
    assert len(await bodies(session)) == 2

    third = await receive(session)
    await process(session)
    assert (await bodies(session))[-1] in any_lang("rate_limited")
    assert await inbox_status(session, third) == "ignored"  # costs nothing

    fourth = await receive(session)
    await process(session)
    assert len(await bodies(session)) == 3  # silence: no second notice
    assert await inbox_status(session, fourth) == "ignored"


# ---- pause


async def test_paused_customer_gets_one_acknowledgement_then_silence(session: AsyncSession) -> None:
    cid = await conversation_id(session)
    await handoff_service.pause(session, cid, hours=1)

    first = await receive(session, "hello?")
    await process(session)
    assert await bodies(session) and (await bodies(session))[0] in any_lang("paused_ack")
    assert await inbox_status(session, first) == "ignored"

    await receive(session, "anyone there?")
    await process(session)
    assert len(await bodies(session)) == 1  # silence

    kept = await session.execute(select(Message.body).where(Message.conversation_id == cid))
    assert {row[0] for row in kept.all()} == {"hello?", "anyone there?"}  # staff can read them


async def test_the_bot_answers_again_when_the_pause_has_ended(session: AsyncSession) -> None:
    cid = await conversation_id(session)
    await handoff_service.pause(session, cid, hours=1)
    conv = await repo.load_state(session, cid)
    conv.bot_paused_until = datetime.now(UTC) - timedelta(minutes=1)
    await repo.save_state(session, cid, conv)

    await receive(session)
    await process(session)

    assert (await bodies(session))[-1] in any_lang("ask_detail")
    assert not handoff_service.is_paused(await repo.load_state(session, cid))


# ---- handoff after a turn


async def three_unclear_messages(session: AsyncSession) -> worker.TurnOutcome:
    outcome = worker.TurnOutcome()
    for _ in range(3):  # the third unclear message goes to a person
        await receive(session)
        outcome = await process(session)
    return outcome


async def test_a_handoff_turn_writes_the_record_and_pauses_the_bot(session: AsyncSession) -> None:
    outcome = await three_unclear_messages(session)

    assert outcome.new_handoff is True
    assert (await bodies(session))[-1] in any_lang("handoff_created")
    rows = await handoffs(session)
    assert len(rows) == 1 and rows[0].status == "open"
    assert handoff_service.is_paused(await repo.load_state(session, await conversation_id(session)))

    await receive(session, "hello?")  # while paused: one acknowledgement
    await process(session)
    assert (await bodies(session))[-1] in any_lang("paused_ack")


async def test_after_hours_the_customer_is_told_when_the_team_is_back(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "after_hours_note", lambda lang, settings: "TEAM IS CLOSED")
    await three_unclear_messages(session)

    sent = await bodies(session)
    assert sent[-2] in any_lang("handoff_created")
    assert sent[-1] == "TEAM IS CLOSED"


# ---- daily budget


async def test_budget_used_up_means_handoff_only(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch, daily_cost_budget=1.0, cost_per_text_message=1.0)

    first = await receive(session, "price of GA-2100?")
    outcome = await process(session)

    assert await inbox_status(session, first) == "ignored"  # no model call, no cost
    assert (await bodies(session)) and (await bodies(session))[0] in any_lang("handoff_created")
    assert [row.reason for row in await handoffs(session)] == ["budget_reached"]
    assert outcome.new_handoff is True
    assert len(outcome.alerts) == 1 and "budget" in outcome.alerts[0][0].lower()

    await receive(session, "hello?")
    again = await process(session)
    assert len(await bodies(session)) == 1  # told once a day, then silence
    assert len(await handoffs(session)) == 1  # one ticket, not one per message
    assert again.alerts == [] and again.new_handoff is False  # the owner is told once


# ---- card numbers


async def test_a_card_number_gets_a_warning_and_is_never_stored(session: AsyncSession) -> None:
    inbox_id = await receive(session, "my card is 4111 1111 1111 1111")
    await process(session)

    assert (await bodies(session))[0] in any_lang("card_warning")
    assert await inbox_status(session, inbox_id) == "ignored"
    stored = await session.execute(
        select(Message.body).where(Message.conversation_id == await conversation_id(session))
    )
    inbox_text = await session.execute(select(WhatsAppInbox.text))
    for (text,) in [*stored.all(), *inbox_text.all()]:
        assert "4111" not in (text or "")
