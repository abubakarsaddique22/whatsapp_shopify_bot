"""Phase 11, step 5: handoff/service.py against real Postgres.

The handoff record, one open handoff per conversation, the summary, pause, resume and the
handoff list. Each test works inside a transaction that is rolled back at the end.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.settings import get_settings
from app.db import repositories as repo
from app.db.tables import Conversation, Handoff
from app.domain.models import ConversationState, Lang
from app.handoff import service

WA_ID = "920000000021"
HASH = "testhash-ho"


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


async def new_conversation(
    session: AsyncSession, wa_id: str = WA_ID, wa_hash: str = HASH
) -> tuple[int, ConversationState]:
    conversation_id = await repo.get_or_create_conversation(session, wa_id, wa_hash)
    return conversation_id, await repo.load_state(session, conversation_id)


def talking(conv: ConversationState, *customer_lines: str) -> ConversationState:
    for text in customer_lines:
        conv.history += [{"role": "customer", "text": text}, {"role": "bot", "text": "ok"}]
    return conv


async def rows_of(session: AsyncSession, conversation_id: int) -> list[Handoff]:
    result = await session.execute(
        select(Handoff).where(Handoff.conversation_id == conversation_id).order_by(Handoff.id)
    )
    return list(result.scalars())


# ---- the record


async def test_a_handoff_writes_one_open_row_with_a_summary(session):
    conversation_id, conv = await new_conversation(session)
    conv.language = Lang.ROMAN_UR
    talking(conv, "mujhe kisi insaan se baat karni hai")

    record = await service.create_handoff(session, conversation_id, conv, "human_request")

    assert record.created is True
    (row,) = await rows_of(session, conversation_id)
    assert row.id == record.handoff_id
    assert (row.status, row.urgency, row.reason) == ("open", "normal", "human_request")
    assert row.language == "roman_ur"
    assert row.resolved_at is None and row.notified_at is None
    assert "Reason: human_request" in row.summary
    assert "Customer said: mujhe kisi insaan se baat karni hai" in row.summary


async def test_the_summary_names_the_product_being_discussed(session, monkeypatch):
    conversation_id, conv = await new_conversation(session)
    conv.current_product_id = "gid://shopify/Product/9401"

    async def basics(db, product_id):
        assert product_id == "gid://shopify/Product/9401"
        return SimpleNamespace(title="Qorvex Reef")

    monkeypatch.setattr(repo, "product_basics", basics)

    await service.create_handoff(session, conversation_id, conv, "complaint")

    (row,) = await rows_of(session, conversation_id)
    assert "Product: Qorvex Reef" in row.summary
    assert row.product_id == "gid://shopify/Product/9401"
    assert row.urgency == "high"  # a complaint is urgent


async def test_a_product_that_is_gone_from_the_mirror_does_not_stop_the_handoff(
    session, monkeypatch
):
    conversation_id, conv = await new_conversation(session)
    conv.current_product_id = "gid://shopify/Product/1"

    async def gone(db, product_id):
        return None

    monkeypatch.setattr(repo, "product_basics", gone)

    record = await service.create_handoff(session, conversation_id, conv, "human_request")

    assert record.created is True
    (row,) = await rows_of(session, conversation_id)
    assert "Product: none discussed" in row.summary


async def test_an_explicit_urgency_and_a_note_are_kept(session):
    conversation_id, conv = await new_conversation(session)

    await service.create_handoff(
        session, conversation_id, conv, "human_request", urgency="high", note="wants a call"
    )

    (row,) = await rows_of(session, conversation_id)
    assert row.urgency == "high"
    assert "Note: wants a call" in row.summary


async def test_a_card_number_never_reaches_the_summary(session):
    conversation_id, conv = await new_conversation(session)
    talking(conv, "my card is 4111 1111 1111 1111")

    await service.create_handoff(session, conversation_id, conv, "private_data_request")

    (row,) = await rows_of(session, conversation_id)
    assert "4111" not in row.summary and "[CARD]" in row.summary


async def test_a_long_reason_is_cut_to_the_column_size(session):
    conversation_id, conv = await new_conversation(session)

    await service.create_handoff(session, conversation_id, conv, "r" * 100)

    (row,) = await rows_of(session, conversation_id)
    assert len(row.reason) == service.MAX_REASON_CHARS


async def test_asking_again_while_a_handoff_is_open_makes_no_second_ticket(session):
    conversation_id, conv = await new_conversation(session)

    first = await service.create_handoff(session, conversation_id, conv, "human_request")
    second = await service.create_handoff(session, conversation_id, conv, "complaint")

    assert (first.created, second.created) == (True, False)
    assert second.handoff_id == first.handoff_id
    (row,) = await rows_of(session, conversation_id)
    assert row.reason == "human_request"  # the first request stays as it was


async def test_another_customer_has_a_handoff_of_their_own(session):
    first_id, first_conv = await new_conversation(session)
    other_id, other_conv = await new_conversation(session, "920000000022", "testhash-ho2")

    one = await service.create_handoff(session, first_id, first_conv, "human_request")
    two = await service.create_handoff(session, other_id, other_conv, "human_request")

    assert one.created and two.created
    assert one.handoff_id != two.handoff_id


# ---- pause


async def test_pause_sets_bot_paused_until_for_the_configured_hours(session):
    conversation_id, _ = await new_conversation(session)
    now = datetime.now(UTC)

    until = await service.pause(session, conversation_id, now=now)

    assert until == now + timedelta(hours=get_settings().handoff_pause_hours)
    saved = await repo.load_state(session, conversation_id)
    assert saved.bot_paused_until == until
    assert service.is_paused(saved, now) is True


async def test_pause_takes_its_own_number_of_hours(session):
    conversation_id, _ = await new_conversation(session)
    now = datetime.now(UTC)

    until = await service.pause(session, conversation_id, hours=2, now=now)

    assert until == now + timedelta(hours=2)


async def test_pause_refuses_less_than_one_hour(session):
    conversation_id, _ = await new_conversation(session)
    with pytest.raises(ValueError):
        await service.pause(session, conversation_id, hours=0)


async def test_pause_of_an_unknown_conversation_is_a_lookup_error(session):
    with pytest.raises(LookupError):
        await service.pause(session, 2_000_000_000)


# ---- resume


async def paused_with_handoff(session: AsyncSession) -> tuple[int, int]:
    """A conversation that went through a handoff the way the graph leaves it."""
    conversation_id, conv = await new_conversation(session)
    conv.needs_handoff = True
    conv.clarification_count = 3
    conv.bot_paused_until = datetime.now(UTC) + timedelta(hours=12)
    await repo.save_state(session, conversation_id, conv)
    record = await service.create_handoff(session, conversation_id, conv, "unclear")
    return conversation_id, record.handoff_id


async def test_resume_clears_the_pause_the_flag_and_the_question_count(session):
    conversation_id, _ = await paused_with_handoff(session)

    result = await service.resume(session, conversation_id)

    assert result.was_paused is True
    saved = await repo.load_state(session, conversation_id)
    assert saved.bot_paused_until is None
    assert saved.needs_handoff is False
    assert saved.clarification_count == 0
    assert service.is_paused(saved) is False


async def test_resume_resolves_the_open_handoff(session):
    conversation_id, handoff_id = await paused_with_handoff(session)

    result = await service.resume(session, conversation_id)

    assert result.handoffs_resolved == 1
    (row,) = await rows_of(session, conversation_id)
    assert row.id == handoff_id
    assert row.status == "resolved"
    assert row.resolved_at is not None


async def test_resume_twice_is_harmless(session):
    conversation_id, _ = await paused_with_handoff(session)
    await service.resume(session, conversation_id)

    again = await service.resume(session, conversation_id)

    assert (again.was_paused, again.handoffs_resolved) == (False, 0)


async def test_resume_keeps_the_rest_of_the_conversation(session):
    conversation_id, conv = await new_conversation(session)
    conv.language = Lang.EN
    conv.current_product_id = "gid://shopify/Product/9401"
    conv.bot_paused_until = datetime.now(UTC) + timedelta(hours=1)
    await repo.save_state(session, conversation_id, conv)

    await service.resume(session, conversation_id)

    saved = await repo.load_state(session, conversation_id)
    assert saved.language is Lang.EN
    assert saved.current_product_id == "gid://shopify/Product/9401"


async def test_a_new_handoff_is_possible_after_the_old_one_was_resolved(session):
    conversation_id, old_id = await paused_with_handoff(session)
    await service.resume(session, conversation_id)
    conv = await repo.load_state(session, conversation_id)

    record = await service.create_handoff(session, conversation_id, conv, "complaint")

    assert record.created is True
    assert record.handoff_id != old_id
    statuses = [row.status for row in await rows_of(session, conversation_id)]
    assert statuses == ["resolved", "open"]


async def test_resume_does_not_touch_another_customers_handoff(session):
    mine, _ = await paused_with_handoff(session)
    other_id, other_conv = await new_conversation(session, "920000000022", "testhash-ho2")
    await service.create_handoff(session, other_id, other_conv, "human_request")

    await service.resume(session, mine)

    (other_row,) = await rows_of(session, other_id)
    assert other_row.status == "open"


async def test_resume_of_an_unknown_conversation_is_a_lookup_error(session):
    with pytest.raises(LookupError):
        await service.resume(session, 2_000_000_000)


# ---- the timer


async def test_resume_if_expired_leaves_a_running_pause_alone(session):
    conversation_id, _ = await paused_with_handoff(session)
    conv = await repo.load_state(session, conversation_id)

    assert await service.resume_if_expired(session, conversation_id, conv) is False

    assert conv.bot_paused_until is not None
    assert (await rows_of(session, conversation_id))[0].status == "open"


async def test_resume_if_expired_does_nothing_without_a_pause(session):
    conversation_id, conv = await new_conversation(session)
    assert await service.resume_if_expired(session, conversation_id, conv) is False


async def test_resume_if_expired_resumes_when_the_pause_has_ended(session):
    conversation_id, _ = await paused_with_handoff(session)
    conv = await repo.load_state(session, conversation_id)
    later = conv.bot_paused_until + timedelta(minutes=1)  # type: ignore[operator]

    assert await service.resume_if_expired(session, conversation_id, conv, later) is True

    assert conv.bot_paused_until is None  # the caller's copy is resumed too
    assert conv.needs_handoff is False and conv.clarification_count == 0
    saved = await repo.load_state(session, conversation_id)
    assert saved.bot_paused_until is None and saved.needs_handoff is False
    assert (await rows_of(session, conversation_id))[0].status == "resolved"


# ---- the handoff list


async def test_the_list_shows_open_handoffs_oldest_first_and_no_resolved_ones(session):
    first_id, first_conv = await new_conversation(session)
    second_id, second_conv = await new_conversation(session, "920000000022", "testhash-ho2")
    third_id, third_conv = await new_conversation(session, "920000000023", "testhash-ho3")
    one = await service.create_handoff(session, first_id, first_conv, "human_request")
    two = await service.create_handoff(session, second_id, second_conv, "complaint")
    three = await service.create_handoff(session, third_id, third_conv, "unclear")
    await service.resume(session, second_id)

    rows = await service.list_open(session, limit=500)

    ids = [row.id for row in rows]
    assert one.handoff_id in ids and three.handoff_id in ids
    assert two.handoff_id not in ids  # resolved
    assert ids.index(one.handoff_id) < ids.index(three.handoff_id)
    mine = next(row for row in rows if row.id == one.handoff_id)
    assert mine.conversation_id == first_id
    assert mine.reason == "human_request" and mine.notified_at is None


async def test_the_list_respects_its_limit(session):
    for number in range(3):
        conversation_id, conv = await new_conversation(
            session, f"92000000003{number}", f"testhash-l{number}"
        )
        await service.create_handoff(session, conversation_id, conv, "human_request")

    assert len(await service.list_open(session, limit=2)) == 2


# ---- the table itself


async def test_the_database_allows_only_one_open_row_per_conversation(session):
    conversation_id, _ = await new_conversation(session)
    session.add(Handoff(conversation_id=conversation_id, reason="a", status="open"))
    await session.flush()
    session.add(Handoff(conversation_id=conversation_id, reason="b", status="open"))

    with pytest.raises(IntegrityError, match="uq_handoffs_one_open"):
        await session.flush()


async def test_deleting_a_conversation_deletes_its_handoffs(session):
    conversation_id, conv = await new_conversation(session)
    await service.create_handoff(session, conversation_id, conv, "human_request")

    await session.execute(delete(Conversation).where(Conversation.id == conversation_id))

    count = await session.execute(
        select(func.count()).select_from(Handoff).where(Handoff.conversation_id == conversation_id)
    )
    assert count.scalar_one() == 0
