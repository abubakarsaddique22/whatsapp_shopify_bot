"""Phase 11, step 9: the retention job and the erase-one-customer code.

Real Postgres; every test is rolled back, so the real tables are not changed. The tests look at
their OWN rows only (the dev database may hold other old rows that the job also deletes).
Needs: docker compose up -d db, and uv run alembic upgrade head.
The checkpoint tests are skipped until the LangGraph tables exist (the worker creates them).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.settings import Settings, get_settings
from app.db.tables import (
    Conversation,
    Handoff,
    Message,
    SecurityEvent,
    SyncEvent,
    WhatsAppInbox,
    WhatsAppOutbox,
)
from app.jobs.retention import run_retention
from app.security import erasure

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
SETTINGS = Settings(_env_file=None)  # type: ignore[call-arg]  # the default retention days


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


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


# ---- helpers


def unique() -> str:
    return uuid.uuid4().hex[:10]


async def conversation(session: AsyncSession, idle_days: float = 1) -> tuple[int, str]:
    """A conversation whose customer last wrote `idle_days` ago. Returns (id, wa_id_hash)."""
    tag = unique()
    conv = Conversation(
        wa_id=f"92{tag}",
        wa_id_hash=f"h{tag}",
        state={},
        last_inbound_at=ago(idle_days),
        created_at=ago(idle_days),
    )
    session.add(conv)
    await session.flush()
    return conv.id, conv.wa_id_hash


async def present(session: AsyncSession, model: Any, pk: int) -> bool:
    result = await session.execute(select(model.id).where(model.id == pk))
    return result.scalar_one_or_none() is not None


async def add(session: AsyncSession, row: Any) -> int:
    session.add(row)
    await session.flush()
    return int(row.id)


async def message(session: AsyncSession, cid: int, age_days: float = 1) -> int:
    return await add(
        session, Message(conversation_id=cid, direction="in", body="hi", created_at=ago(age_days))
    )


async def inbox(session: AsyncSession, cid: int, h: str, status: str, age_days: float) -> int:
    return await add(
        session,
        WhatsAppInbox(
            wamid=f"wamid.{unique()}",
            conversation_id=cid,
            wa_id_hash=h,
            msg_type="text",
            text="hi",
            status=status,
            received_at=ago(age_days),
        ),
    )


async def outbox(session: AsyncSession, cid: int, status: str, age_days: float) -> int:
    return await add(
        session,
        WhatsAppOutbox(
            conversation_id=cid, seq=0, body="hello", status=status, created_at=ago(age_days)
        ),
    )


async def handoff(session: AsyncSession, cid: int, status: str, age_days: float) -> int:
    return await add(
        session,
        Handoff(
            conversation_id=cid,
            reason="human_request",
            status=status,
            created_at=ago(age_days),
            resolved_at=ago(age_days) if status == "resolved" else None,
        ),
    )


async def event(session: AsyncSession, kind: str, age_days: float, **fields: Any) -> int:
    return await add(session, SecurityEvent(kind=kind, created_at=ago(age_days), **fields))


async def sync_event(session: AsyncSession, status: str, age_days: float) -> int:
    return await add(
        session,
        SyncEvent(
            webhook_id=f"hook-{unique()}",
            topic="products/update",
            shop_domain="shop.example.com",
            status=status,
            received_at=ago(age_days),
        ),
    )


async def retention(session: AsyncSession, *, dry_run: bool = False) -> dict[str, int]:
    return (await run_retention(session, SETTINGS, now=NOW, dry_run=dry_run)).counts


async def wa_id_of(session: AsyncSession, cid: int) -> str:
    result = await session.execute(select(Conversation.wa_id).where(Conversation.id == cid))
    return str(result.scalar_one())


async def checkpoints_exist(session: AsyncSession) -> bool:
    return "checkpoints" in await erasure.checkpoint_tables(session)


async def add_checkpoint(session: AsyncSession, cid: int) -> None:
    insert = (
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
        "VALUES (:thread, '', :cp, CAST('{}' AS jsonb), CAST('{}' AS jsonb))"
    )
    await session.execute(
        text(insert), {"thread": erasure.checkpoint_thread_id(cid), "cp": unique()}
    )


async def checkpoint_rows(session: AsyncSession, cid: int) -> int:
    result = await session.execute(
        text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread"),
        {"thread": erasure.checkpoint_thread_id(cid)},
    )
    return int(result.scalar_one())


# ---- retention: conversations


async def test_a_silent_customer_is_erased_with_everything_under_the_conversation(
    session: AsyncSession,
) -> None:
    old_id, old_hash = await conversation(session, idle_days=200)  # past 180 days
    kept_id, _ = await conversation(session, idle_days=10)
    old_message = await message(session, old_id, age_days=1)
    old_inbox = await inbox(session, old_id, old_hash, "done", age_days=1)
    old_outbox = await outbox(session, old_id, "sent", age_days=1)
    old_handoff = await handoff(session, old_id, "resolved", age_days=1)
    kept_message = await message(session, kept_id)

    counts = await retention(session)

    assert counts["conversations"] >= 1
    assert not await present(session, Conversation, old_id)
    for model, pk in (
        (Message, old_message),
        (WhatsAppInbox, old_inbox),
        (WhatsAppOutbox, old_outbox),
        (Handoff, old_handoff),
    ):
        assert not await present(session, model, pk)  # went with the conversation (cascade)
    assert await present(session, Conversation, kept_id)
    assert await present(session, Message, kept_message)  # the second customer is untouched


async def test_a_silent_customer_with_an_open_handoff_stays(session: AsyncSession) -> None:
    cid, _ = await conversation(session, idle_days=400)
    await handoff(session, cid, "open", age_days=1)

    await retention(session)

    assert await present(session, Conversation, cid)  # staff still work on it


async def test_old_transcript_lines_go_but_the_conversation_stays(session: AsyncSession) -> None:
    cid, _ = await conversation(session, idle_days=5)
    old_line = await message(session, cid, age_days=100)  # past 90 days
    new_line = await message(session, cid, age_days=89)

    await retention(session)

    assert await present(session, Conversation, cid)
    assert not await present(session, Message, old_line)
    assert await present(session, Message, new_line)


# ---- retention: queues, handoffs, events


async def test_finished_queue_rows_expire_and_pending_ones_never(session: AsyncSession) -> None:
    cid, h = await conversation(session, idle_days=5)
    old_done = await inbox(session, cid, h, "done", age_days=40)  # past 30 days
    old_failed = await inbox(session, cid, h, "failed", age_days=40)
    old_pending = await inbox(session, cid, h, "pending", age_days=40)
    old_processing = await inbox(session, cid, h, "processing", age_days=40)
    new_done = await inbox(session, cid, h, "done", age_days=5)
    old_sent = await outbox(session, cid, "sent", age_days=40)
    old_waiting = await outbox(session, cid, "pending", age_days=40)

    await retention(session)

    assert not await present(session, WhatsAppInbox, old_done)
    assert not await present(session, WhatsAppInbox, old_failed)
    assert await present(session, WhatsAppInbox, old_pending)
    assert await present(session, WhatsAppInbox, old_processing)
    assert await present(session, WhatsAppInbox, new_done)
    assert not await present(session, WhatsAppOutbox, old_sent)
    assert await present(session, WhatsAppOutbox, old_waiting)


async def test_old_resolved_handoffs_go_and_open_ones_stay(session: AsyncSession) -> None:
    cid, _ = await conversation(session, idle_days=5)
    other, _ = await conversation(session, idle_days=5)
    old_resolved = await handoff(session, cid, "resolved", age_days=100)  # past 90 days
    old_open = await handoff(session, other, "open", age_days=100)

    await retention(session)

    assert not await present(session, Handoff, old_resolved)
    assert await present(session, Handoff, old_open)


async def test_old_security_events_go(session: AsyncSession) -> None:
    old = await event(session, "injection_suspected", age_days=400)  # past 365 days
    recent = await event(session, "injection_suspected", age_days=300)

    await retention(session)

    assert not await present(session, SecurityEvent, old)
    assert await present(session, SecurityEvent, recent)


async def test_sync_events_expire_by_status(session: AsyncSession) -> None:
    old_done = await sync_event(session, "done", age_days=20)  # past 14 days
    old_ignored = await sync_event(session, "ignored", age_days=20)
    new_done = await sync_event(session, "done", age_days=5)
    old_pending = await sync_event(session, "pending", age_days=20)
    dead_recent = await sync_event(session, "failed", age_days=60)  # dead letter: 90 days
    dead_old = await sync_event(session, "failed", age_days=120)

    await retention(session)

    assert not await present(session, SyncEvent, old_done)
    assert not await present(session, SyncEvent, old_ignored)
    assert await present(session, SyncEvent, new_done)
    assert await present(session, SyncEvent, old_pending)
    assert await present(session, SyncEvent, dead_recent)
    assert not await present(session, SyncEvent, dead_old)


async def test_a_dry_run_counts_but_changes_nothing(session: AsyncSession) -> None:
    cid, _ = await conversation(session, idle_days=200)
    line = await message(session, cid)

    counts = await retention(session, dry_run=True)

    assert counts["conversations"] >= 1  # the numbers of a real run
    assert await present(session, Conversation, cid)
    assert await present(session, Message, line)


# ---- retention: LangGraph checkpoints


async def test_checkpoints_of_idle_conversations_go_and_active_ones_stay(
    session: AsyncSession,
) -> None:
    if not await checkpoints_exist(session):
        pytest.skip("LangGraph checkpoint tables do not exist yet (start the worker once)")
    idle, _ = await conversation(session, idle_days=20)  # silent for more than 7 days
    active, _ = await conversation(session, idle_days=2)
    await add_checkpoint(session, idle)
    await add_checkpoint(session, active)

    await retention(session)

    assert await checkpoint_rows(session, idle) == 0
    assert await checkpoint_rows(session, active) == 1


# ---- erase one customer


async def test_erasing_one_customer_leaves_the_others_alone(session: AsyncSession) -> None:
    cid, h = await conversation(session)
    other, other_hash = await conversation(session)
    mine = [
        (Message, await message(session, cid)),
        (WhatsAppInbox, await inbox(session, cid, h, "done", age_days=1)),
        (WhatsAppOutbox, await outbox(session, cid, "sent", age_days=1)),
        (Handoff, await handoff(session, cid, "open", age_days=1)),
        (SecurityEvent, await event(session, "injection_suspected", 1, wa_hash=h)),
        (
            SecurityEvent,
            await event(session, "budget_notice", 1, ref=str(cid), detail={"day": "2026-10-18"}),
        ),
    ]
    theirs = [
        (Message, await message(session, other)),
        (SecurityEvent, await event(session, "injection_suspected", 1, wa_hash=other_hash)),
    ]

    counts = await erasure.erase_conversation(session, cid)

    assert counts["messages"] == 1 and counts["handoffs"] == 1 and counts["security_events"] == 2
    assert not await present(session, Conversation, cid)
    for model, pk in mine:
        assert not await present(session, model, pk)
    assert await present(session, Conversation, other)
    for model, pk in theirs:
        assert await present(session, model, pk)


async def test_erasing_leaves_an_audit_row_without_any_personal_data(
    session: AsyncSession,
) -> None:
    cid, h = await conversation(session)
    wa_id = await wa_id_of(session, cid)

    await erasure.erase_conversation(session, cid)

    rows = await session.execute(
        select(SecurityEvent.wa_hash, SecurityEvent.detail).where(
            SecurityEvent.kind == "customer_erased"
        )
    )
    mine = [(h_, d) for h_, d in rows.all() if d.get("conversation_id") == cid]
    assert len(mine) == 1
    audit_hash, detail = mine[0]
    assert audit_hash is None
    assert wa_id not in str(detail) and h not in str(detail)


async def test_the_counts_of_a_dry_run_match_what_an_erase_removes(session: AsyncSession) -> None:
    cid, _ = await conversation(session)
    await message(session, cid)
    await message(session, cid)
    await handoff(session, cid, "open", age_days=1)

    before = await erasure.count_conversation_data(session, cid)
    assert await present(session, Conversation, cid)  # counting deletes nothing
    after = await erasure.erase_conversation(session, cid)

    assert before == after
    assert before["messages"] == 2 and before["handoffs"] == 1


async def test_find_conversation_by_number_and_by_id(session: AsyncSession) -> None:
    cid, h = await conversation(session)
    wa_id = await wa_id_of(session, cid)

    assert await erasure.find_conversation(session, wa_id=wa_id) == (cid, h)
    assert await erasure.find_conversation(session, conversation_id=cid) == (cid, h)
    assert await erasure.find_conversation(session, wa_id="0000") is None
    with pytest.raises(ValueError):
        await erasure.find_conversation(session)
    with pytest.raises(LookupError):
        await erasure.erase_conversation(session, 987654321)


async def test_erasing_a_customer_removes_the_checkpoints_too(session: AsyncSession) -> None:
    if not await checkpoints_exist(session):
        pytest.skip("LangGraph checkpoint tables do not exist yet (start the worker once)")
    cid, _ = await conversation(session)
    other, _ = await conversation(session)
    await add_checkpoint(session, cid)
    await add_checkpoint(session, other)

    counts = await erasure.erase_conversation(session, cid)

    assert counts["checkpoints"] == 1
    assert await checkpoint_rows(session, cid) == 0
    assert await checkpoint_rows(session, other) == 1
