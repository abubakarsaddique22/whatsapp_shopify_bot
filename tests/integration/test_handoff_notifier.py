"""Phase 11, step 6: handoff/notifier.py against real Postgres.

`notify_pending`: who is told, who is skipped, what a failure leaves behind. The channel itself is
replaced by a recorder (the channels are tested in tests/unit/test_handoff_notifier.py). Each test
works inside a transaction that is rolled back at the end.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import ConfigError, NotificationError
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.db.tables import Handoff
from app.handoff import notifier, service

LOT = 500  # a limit high enough to see every row of the test, whatever else is in the database


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


class Channel:
    """Stands in for notifier.send: records what staff would have read, can be told to fail."""

    def __init__(self) -> None:
        self.sent: list[notifier.Notification] = []
        self.fail_for: set[int] = set()  # handoff ids whose message must fail
        self.error: Exception = NotificationError("slack down (HTTP 500)")

    async def __call__(self, notification, settings=None) -> None:
        if any(f"#{handoff_id}]" in notification.subject for handoff_id in self.fail_for):
            raise self.error
        self.sent.append(notification)

    def about(self, handoff_id: int) -> list[notifier.Notification]:
        return [n for n in self.sent if f"#{handoff_id}]" in n.subject]


@pytest.fixture
def channel(monkeypatch) -> Channel:
    fake = Channel()
    monkeypatch.setattr(notifier, "send", fake)
    return fake


async def new_handoff(
    session: AsyncSession, number: int, reason: str = "human_request"
) -> tuple[int, int]:
    """(conversation_id, handoff_id) for a customer with the number 92000000005<number>."""
    conversation_id = await repo.get_or_create_conversation(
        session, f"92000000005{number}", f"testhash-n{number}"
    )
    conv = await repo.load_state(session, conversation_id)
    conv.history += [{"role": "customer", "text": "mujhe kisi insaan se baat karni hai"}]
    record = await service.create_handoff(session, conversation_id, conv, reason)
    return conversation_id, record.handoff_id


async def row_of(session: AsyncSession, handoff_id: int) -> Handoff:
    result = await session.execute(select(Handoff).where(Handoff.id == handoff_id))
    row = result.scalar_one()
    await session.refresh(row)
    return row


SETTINGS = Settings.model_construct(handoff_channel="log", handoff_target="")


async def test_an_open_handoff_is_sent_once_and_marked_as_notified(session, channel):
    _, handoff_id = await new_handoff(session, 1)

    result = await notifier.notify_pending(session, SETTINGS, limit=LOT)

    assert result.sent >= 1 and result.failed == 0
    (n,) = channel.about(handoff_id)
    assert f"Handoff #{handoff_id}" in n.body
    assert "mujhe kisi insaan se baat karni hai" in n.body  # the summary of the service
    row = await row_of(session, handoff_id)
    assert row.notified_at is not None and row.notify_error is None


async def test_staff_see_only_the_end_of_the_customer_number(session, channel):
    _, handoff_id = await new_handoff(session, 2)

    await notifier.notify_pending(session, SETTINGS, limit=LOT)

    (n,) = channel.about(handoff_id)
    assert "number ending 0052" in n.body
    assert "920000000052" not in n.body


async def test_a_second_pass_does_not_tell_staff_again(session, channel):
    _, handoff_id = await new_handoff(session, 3)
    await notifier.notify_pending(session, SETTINGS, limit=LOT)

    again = await notifier.notify_pending(session, SETTINGS, limit=LOT)

    assert len(channel.about(handoff_id)) == 1
    assert again.failed == 0


async def test_a_failure_keeps_the_handoff_pending_with_the_reason(session, channel):
    _, handoff_id = await new_handoff(session, 4)
    channel.fail_for.add(handoff_id)

    result = await notifier.notify_pending(session, SETTINGS, limit=LOT)

    assert result.failed >= 1
    row = await row_of(session, handoff_id)
    assert row.notified_at is None
    assert row.notify_error == "slack down (HTTP 500)"
    assert row.status == "open"  # the handoff itself is not lost


async def test_the_next_pass_retries_and_clears_the_error(session, channel):
    _, handoff_id = await new_handoff(session, 5)
    channel.fail_for.add(handoff_id)
    await notifier.notify_pending(session, SETTINGS, limit=LOT)

    channel.fail_for.clear()
    await notifier.notify_pending(session, SETTINGS, limit=LOT)

    row = await row_of(session, handoff_id)
    assert row.notified_at is not None and row.notify_error is None
    assert len(channel.about(handoff_id)) == 1


async def test_one_failing_handoff_does_not_stop_the_next_one(session, channel):
    _, bad_id = await new_handoff(session, 6)
    _, good_id = await new_handoff(session, 7)
    channel.fail_for.add(bad_id)

    await notifier.notify_pending(session, SETTINGS, limit=LOT)

    assert (await row_of(session, bad_id)).notified_at is None
    assert (await row_of(session, good_id)).notified_at is not None


async def test_a_wrong_channel_setting_is_recorded_not_raised(session, channel):
    _, handoff_id = await new_handoff(session, 8)
    channel.error = ConfigError("Set HANDOFF_TARGET to the address that should get it")
    channel.fail_for.add(handoff_id)

    await notifier.notify_pending(session, SETTINGS, limit=LOT)

    row = await row_of(session, handoff_id)
    assert row.notified_at is None and "HANDOFF_TARGET" in row.notify_error


async def test_an_unexpected_bug_in_a_channel_is_recorded_without_its_text(session, channel):
    _, handoff_id = await new_handoff(session, 9)
    channel.error = RuntimeError("secret detail that must not be stored")
    channel.fail_for.add(handoff_id)

    result = await notifier.notify_pending(session, SETTINGS, limit=LOT)

    assert result.failed >= 1
    row = await row_of(session, handoff_id)
    assert row.notify_error == "unexpected error (RuntimeError)"


async def test_a_handoff_resolved_before_anyone_was_told_is_skipped(session, channel):
    conversation_id, handoff_id = await new_handoff(session, 10)
    await service.resume(session, conversation_id)

    await notifier.notify_pending(session, SETTINGS, limit=LOT)

    assert channel.about(handoff_id) == []
    assert (await row_of(session, handoff_id)).notified_at is None


async def test_the_urgent_handoff_is_marked_urgent(session, channel):
    _, handoff_id = await new_handoff(session, 11, reason="complaint")

    await notifier.notify_pending(session, SETTINGS, limit=LOT)

    (n,) = channel.about(handoff_id)
    assert n.urgent is True and "URGENT" in n.subject


async def test_the_limit_caps_one_pass(session, channel):
    for number in (12, 13, 14):
        await new_handoff(session, number)

    result = await notifier.notify_pending(session, SETTINGS, limit=2)

    assert result.sent + result.failed == 2
