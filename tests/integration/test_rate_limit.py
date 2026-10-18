"""Rate limits, strikes and the daily budget (Phase 11, step 4; Blueprint 9.6).

Each test works inside a transaction that is rolled back at the end.
Needs: docker compose up -d db, and uv run alembic upgrade head.

The budget counts the messages of ALL senders, so its tests use a date far in the future: rows
from your real chats are older than 24 hours there and cannot change the numbers.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.db.tables import SecurityEvent, WhatsAppInbox
from app.security.rate_limit import STRIKE_KINDS, budget_status, check_sender

WA_ID = "920000000012"
HASH = "testhash-rl"


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


def limits(**changes) -> Settings:
    values = {"rate_limit_per_minute": 3, "rate_limit_per_hour": 10, "rate_limit_per_day": 50}
    return Settings(_env_file=None, **(values | changes))


async def new_conversation(session: AsyncSession, wa_id: str = WA_ID, wa_hash: str = HASH) -> int:
    return await repo.get_or_create_conversation(session, wa_id, wa_hash)


async def add_messages(
    session: AsyncSession,
    conversation_id: int,
    count: int,
    *,
    now: datetime,
    age: timedelta = timedelta(0),
    msg_type: str = "text",
    status: str = "done",
    wa_hash: str = HASH,
) -> None:
    for _ in range(count):
        session.add(
            WhatsAppInbox(
                wamid=f"wamid.test-{uuid.uuid4().hex}",
                conversation_id=conversation_id,
                wa_id_hash=wa_hash,
                msg_type=msg_type,
                status=status,
                received_at=now - age,
            )
        )
    await session.flush()


async def add_events(
    session: AsyncSession, kind: str, count: int, *, now: datetime, age: timedelta
) -> None:
    for _ in range(count):
        session.add(SecurityEvent(kind=kind, wa_hash=HASH, detail={}, created_at=now - age))
    await session.flush()


async def events_of(session: AsyncSession, kind: str, wa_hash: str = HASH) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(SecurityEvent)
        .where(SecurityEvent.kind == kind, SecurityEvent.wa_hash == wa_hash)
    )
    return int(result.scalar_one())


# ---- per-sender limits


async def test_a_quiet_sender_is_allowed(session):
    now = datetime.now(UTC)
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 2, now=now)

    decision = await check_sender(session, conversation, HASH, limits(), now)

    assert (decision.action, decision.reason) == ("allow", None)
    assert await events_of(session, "rate_limited") == 0


async def test_over_the_minute_limit_gets_one_notice_then_silence(session):
    now = datetime.now(UTC)
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 4, now=now)  # the limit is 3 a minute

    first = await check_sender(session, conversation, HASH, limits(), now)
    second = await check_sender(session, conversation, HASH, limits(), now)

    assert (first.action, first.reason) == ("notify", "minute")
    assert (second.action, second.reason) == ("silent", "minute")
    assert await events_of(session, "rate_limited") == 1


async def test_the_database_clock_is_used_when_no_time_is_given(session):
    conversation = await new_conversation(session)
    for _ in range(4):  # received_at comes from the database, as in production
        session.add(
            WhatsAppInbox(
                wamid=f"wamid.test-{uuid.uuid4().hex}",
                conversation_id=conversation,
                wa_id_hash=HASH,
                msg_type="text",
                status="done",
            )
        )
    await session.flush()

    first = await check_sender(session, conversation, HASH, limits())
    second = await check_sender(session, conversation, HASH, limits())

    assert (first.action, first.reason) == ("notify", "minute")
    assert second.action == "silent"


async def test_messages_older_than_a_minute_do_not_count_for_the_minute_limit(session):
    now = datetime.now(UTC)
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 5, now=now, age=timedelta(seconds=90))

    decision = await check_sender(session, conversation, HASH, limits(), now)

    assert decision.action == "allow"


async def test_the_hour_limit(session):
    now = datetime.now(UTC)
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 5, now=now, age=timedelta(minutes=10))

    decision = await check_sender(session, conversation, HASH, limits(rate_limit_per_hour=4), now)

    assert (decision.action, decision.reason) == ("notify", "hour")


async def test_the_day_limit(session):
    now = datetime.now(UTC)
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 7, now=now, age=timedelta(hours=3))

    decision = await check_sender(session, conversation, HASH, limits(rate_limit_per_day=6), now)

    assert (decision.action, decision.reason) == ("notify", "day")


async def test_the_sender_is_told_again_after_the_window_has_passed(session):
    now = datetime.now(UTC)
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 4, now=now)
    await add_events(session, "rate_limited", 1, now=now, age=timedelta(minutes=2))

    decision = await check_sender(session, conversation, HASH, limits(), now)

    assert decision.action == "notify"  # the old notice is outside the one-minute window


async def test_the_longest_window_that_is_over_decides_the_silence(session):
    now = datetime.now(UTC)
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 4, now=now)  # over the minute AND the hour limit
    await add_events(session, "rate_limited", 1, now=now, age=timedelta(minutes=5))

    decision = await check_sender(
        session, conversation, HASH, limits(rate_limit_per_minute=2, rate_limit_per_hour=3), now
    )

    assert (decision.action, decision.reason) == ("silent", "hour")


async def test_another_sender_is_not_affected(session):
    now = datetime.now(UTC)
    noisy = await new_conversation(session)
    quiet = await new_conversation(session, "920000000013", "testhash-rl2")
    await add_messages(session, noisy, 4, now=now)
    await add_messages(session, quiet, 1, now=now, wa_hash="testhash-rl2")

    assert (await check_sender(session, noisy, HASH, limits(), now)).action == "notify"
    assert (await check_sender(session, quiet, "testhash-rl2", limits(), now)).action == "allow"


# ---- strikes


async def test_three_strikes_start_a_cool_down(session):
    now = datetime.now(UTC)
    conversation = await new_conversation(session)
    await add_events(session, "injection_suspected", 2, now=now, age=timedelta(minutes=10))
    await add_events(session, "rate_limited", 1, now=now, age=timedelta(minutes=20))

    first = await check_sender(session, conversation, HASH, limits(), now)
    second = await check_sender(session, conversation, HASH, limits(), now)

    assert (first.action, first.reason) == ("notify", "strikes")
    assert (second.action, second.reason) == ("silent", "strikes")
    assert await events_of(session, "cooldown_notice") == 1


async def test_the_cool_down_notice_is_not_a_strike(session):
    now = datetime.now(UTC)
    conversation = await new_conversation(session)
    await add_events(session, "injection_suspected", 3, now=now, age=timedelta(minutes=10))
    since = now - timedelta(hours=1)

    await check_sender(session, conversation, HASH, limits(), now)

    # still 3 strikes: the notice did not add one, so the cool-down cannot extend itself
    assert await repo.count_recent_security_events(session, STRIKE_KINDS, HASH, since) == 3


async def test_two_strikes_are_not_enough(session):
    now = datetime.now(UTC)
    conversation = await new_conversation(session)
    await add_events(session, "injection_suspected", 2, now=now, age=timedelta(minutes=10))

    assert (await check_sender(session, conversation, HASH, limits(), now)).action == "allow"


async def test_strikes_older_than_an_hour_are_forgotten(session):
    now = datetime.now(UTC)
    conversation = await new_conversation(session)
    await add_events(session, "injection_suspected", 5, now=now, age=timedelta(hours=2))

    assert (await check_sender(session, conversation, HASH, limits(), now)).action == "allow"


# ---- the daily budget

FUTURE = datetime(2100, 1, 1, 12, 0, tzinfo=UTC)
DAY = "2100-01-01"


def budget(**changes) -> Settings:
    values = {"daily_cost_budget": 10.0, "cost_per_text_message": 1.0}
    return Settings(_env_file=None, **(values | changes))


async def budget_events(session: AsyncSession, kind: str) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(SecurityEvent)
        .where(
            SecurityEvent.kind == kind,
            SecurityEvent.ref == "daily",
            SecurityEvent.detail["day"].astext == DAY,
        )
    )
    return int(result.scalar_one())


async def test_without_a_budget_the_check_is_off(session):
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 50, now=FUTURE)

    assert await budget_status(session, budget(daily_cost_budget=0), FUTURE) == "off"


async def test_under_80_percent_is_ok_and_writes_nothing(session):
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 7, now=FUTURE)

    assert await budget_status(session, budget(), FUTURE) == "ok"
    assert await budget_events(session, "budget_alarm") == 0
    assert await budget_events(session, "budget_reached") == 0


async def test_at_80_percent_the_alarm_is_written_once(session):
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 8, now=FUTURE)

    assert await budget_status(session, budget(), FUTURE) == "alarm"
    assert await budget_status(session, budget(), FUTURE) == "alarm"
    assert await budget_events(session, "budget_alarm") == 1


async def test_at_100_percent_the_budget_is_exhausted_and_written_once(session):
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 10, now=FUTURE)

    assert await budget_status(session, budget(), FUTURE) == "exhausted"
    assert await budget_status(session, budget(), FUTURE) == "exhausted"
    assert await budget_events(session, "budget_reached") == 1


async def test_a_voice_note_has_its_own_price(session):
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 2, now=FUTURE)
    await add_messages(session, conversation, 2, now=FUTURE, msg_type="audio")

    # 2 texts x 1 + 2 voice notes x 4 = 10 = the whole budget
    status = await budget_status(session, budget(cost_per_voice_message=4.0), FUTURE)

    assert status == "exhausted"


async def test_ignored_messages_cost_nothing(session):
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 20, now=FUTURE, status="ignored")

    assert await budget_status(session, budget(), FUTURE) == "ok"


async def test_messages_older_than_24_hours_are_not_counted(session):
    conversation = await new_conversation(session)
    await add_messages(session, conversation, 20, now=FUTURE, age=timedelta(hours=25))

    assert await budget_status(session, budget(), FUTURE) == "ok"
