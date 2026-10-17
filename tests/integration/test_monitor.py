"""Monitor queries and the dead-letter retry on the real Postgres (Phase 10, step 7).

Everything runs inside a transaction that is rolled back at the end, so the real sync_events and
sync_runs of the dev database are never changed. Test rows use their own webhook ids and the made-up
job kind "test-monitor". Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.settings import get_settings
from app.db import repositories as repo
from app.db.tables import SyncEvent
from app.sync import monitor

KIND = "test-monitor"  # never used by the real jobs


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


async def add_event(
    session: AsyncSession,
    number: int,
    status: str,
    *,
    attempts: int = 0,
    received_at: datetime | None = None,
    next_attempt_at: datetime | None = None,
) -> SyncEvent:
    event = SyncEvent(
        webhook_id=f"test-monitor-{number}",
        topic="products/update",
        shop_domain="test.myshopify.com",
        resource_id="gid://shopify/Product/9990001",
        status=status,
        attempts=attempts,
        error="Shopify is down (test)" if status == "failed" else None,
        next_attempt_at=next_attempt_at,
    )
    if received_at is not None:
        event.received_at = received_at
    session.add(event)
    await session.flush()
    return event


# ---- the dead letter


async def test_a_failed_event_is_listed_in_the_dead_letter(session: AsyncSession) -> None:
    event = await add_event(session, 1, "failed", attempts=5)
    rows = await repo.list_failed_events(session, limit=200)
    mine = [row for row in rows if row.id == event.id]
    assert len(mine) == 1
    assert (mine[0].topic, mine[0].attempts) == ("products/update", 5)
    assert mine[0].error == "Shopify is down (test)"


async def test_the_status_counts_include_a_new_failed_event(session: AsyncSession) -> None:
    before = (await repo.sync_event_status_counts(session)).get("failed", 0)
    await add_event(session, 2, "failed", attempts=5)
    after = (await repo.sync_event_status_counts(session)).get("failed", 0)
    assert after == before + 1


async def test_retry_puts_one_dead_letter_event_back_to_pending(session: AsyncSession) -> None:
    event = await add_event(session, 3, "failed", attempts=5)
    assert await repo.requeue_failed_events(session, event.id) == 1
    await session.refresh(event)
    assert (event.status, event.attempts) == ("pending", 0)
    assert event.next_attempt_at is None
    assert event.processed_at is None


async def test_retry_never_touches_an_event_that_is_not_failed(session: AsyncSession) -> None:
    done = await add_event(session, 4, "done")
    pending = await add_event(session, 5, "pending", attempts=2)
    assert await repo.requeue_failed_events(session, done.id) == 0
    assert await repo.requeue_failed_events(session, pending.id) == 0
    await session.refresh(pending)
    assert pending.attempts == 2  # not reset: it was never in the dead letter


async def test_retry_all_takes_every_dead_letter_event(session: AsyncSession) -> None:
    first = await add_event(session, 6, "failed", attempts=5)
    second = await add_event(session, 7, "failed", attempts=5)
    assert await repo.requeue_failed_events(session) >= 2
    await session.refresh(first)
    await session.refresh(second)
    assert (first.status, second.status) == ("pending", "pending")
    assert (await repo.sync_event_status_counts(session)).get("failed", 0) == 0


async def test_a_requeued_event_can_be_claimed_by_the_worker_again(session: AsyncSession) -> None:
    event = await add_event(session, 8, "failed", attempts=5)
    await repo.requeue_failed_events(session, event.id)
    claimed = await repo.claim_sync_event(session)
    assert claimed is not None  # something is due; the oldest pending row comes first
    assert claimed.status == "pending"


# ---- the backlog


async def test_the_oldest_due_event_is_found(session: AsyncSession) -> None:
    hour_ago = datetime.now(UTC) - timedelta(hours=1)
    await add_event(session, 9, "pending", received_at=hour_ago)
    oldest = await repo.oldest_due_event_time(session)
    assert oldest is not None
    assert oldest <= hour_ago


async def test_an_event_waiting_for_its_retry_is_not_a_backlog(session: AsyncSession) -> None:
    very_old = datetime.now(UTC) - timedelta(days=400)
    later = datetime.now(UTC) + timedelta(hours=1)
    await add_event(session, 10, "pending", received_at=very_old, next_attempt_at=later)
    oldest = await repo.oldest_due_event_time(session)
    assert oldest is None or oldest > very_old


# ---- runs


async def test_a_job_that_never_ran_has_no_run(session: AsyncSession) -> None:
    assert await repo.last_sync_run(session, KIND) is None
    assert await repo.last_ok_run_finished(session, KIND) is None


async def test_the_last_run_and_the_last_good_run_are_told_apart(session: AsyncSession) -> None:
    good_id, _ = await repo.start_sync_run(session, KIND)
    await repo.finish_sync_run(session, good_id, "ok", {}, None, None)
    bad_id, _ = await repo.start_sync_run(session, KIND)
    await repo.finish_sync_run(session, bad_id, "failed", {}, "Shopify is down (test)", None)

    last = await repo.last_sync_run(session, KIND)
    assert last is not None
    assert (last.status, last.error) == ("failed", "Shopify is down (test)")
    assert await repo.last_ok_run_finished(session, KIND) is not None  # the earlier good run


async def test_a_run_that_is_still_running_is_the_last_run(session: AsyncSession) -> None:
    await repo.start_sync_run(session, KIND)
    last = await repo.last_sync_run(session, KIND)
    assert last is not None
    assert last.status == "running"
    assert last.finished_at is None


# ---- all together


async def test_collect_and_evaluate_work_on_the_real_tables(session: AsyncSession) -> None:
    await add_event(session, 11, "failed", attempts=5)
    health = await monitor.collect(session)
    assert health.event_counts.get("failed", 0) >= 1
    assert set(health.runs) <= set(monitor.KINDS)
    alarms = monitor.evaluate(health, datetime.now(UTC), monitor.intervals(get_settings()))
    assert "dead_letter" in [alarm.code for alarm in alarms]
