"""Reconciliation tests (Phase 10, step 6) on the real Postgres.

apply_delta and the sync_runs bookkeeping run inside a transaction that is rolled back at the end.
The run_job tests use a made-up job kind ("test-delta") and delete its rows afterwards, so the real
delta / full / policy history of the dev database is never touched or used.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.errors import ShopifyError
from app.core.settings import get_settings
from app.db import repositories as repo
from app.db.tables import SyncRun
from app.sync import reconcile
from tests.factories import make_product

FIRST = "gid://shopify/Product/9990001"
SECOND = "gid://shopify/Product/9990002"
KIND = "test-delta"  # never used by the real jobs


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


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A real, committing sessionmaker for run_job. Its test rows are deleted at the end."""
    engine = create_async_engine(get_settings().database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield maker
    finally:
        async with maker() as db, db.begin():
            await db.execute(delete(SyncRun).where(SyncRun.kind == KIND))
        await engine.dispose()


async def visible(session: AsyncSession, product_id: str = FIRST) -> bool:
    return await repo.product_basics(session, product_id) is not None


# ---- apply_delta: what a missed webhook leaves behind gets repaired


async def test_delta_adds_a_product_the_webhook_never_reported(session: AsyncSession) -> None:
    counts = await reconcile.apply_delta(session, {FIRST: make_product()})
    assert counts == {"seen": 1, "added": 1, "updated": 0, "unchanged": 0, "hidden": 0}
    assert await visible(session)


async def test_delta_repairs_a_missed_update(session: AsyncSession) -> None:
    await reconcile.apply_delta(session, {FIRST: make_product()})
    counts = await reconcile.apply_delta(session, {FIRST: make_product(title="Renamed Watch")})
    assert counts["updated"] == 1
    basics = await repo.product_basics(session, FIRST)
    assert basics is not None
    assert basics.title == "Renamed Watch"


async def test_delta_leaves_a_product_that_did_not_change(session: AsyncSession) -> None:
    await reconcile.apply_delta(session, {FIRST: make_product()})
    counts = await reconcile.apply_delta(session, {FIRST: make_product()})
    assert (counts["unchanged"], counts["updated"], counts["added"]) == (1, 0, 0)


async def test_delta_hides_a_product_that_is_no_longer_visible(session: AsyncSession) -> None:
    await reconcile.apply_delta(session, {FIRST: make_product()})
    counts = await reconcile.apply_delta(session, {FIRST: None})  # now a draft in Shopify
    assert counts["hidden"] == 1
    assert not await visible(session)


async def test_delta_with_nothing_changed_does_nothing(session: AsyncSession) -> None:
    counts = await reconcile.apply_delta(session, {})
    assert counts == {"seen": 0, "added": 0, "updated": 0, "unchanged": 0, "hidden": 0}


async def test_delta_counts_each_product_on_its_own(session: AsyncSession) -> None:
    await reconcile.apply_delta(session, {FIRST: make_product(), SECOND: make_product(SECOND)})
    counts = await reconcile.apply_delta(
        session,
        {FIRST: make_product(title="Renamed Watch"), SECOND: make_product(SECOND)},
    )
    assert (counts["seen"], counts["updated"], counts["unchanged"]) == (2, 1, 1)
    assert await visible(session, FIRST)
    assert await visible(session, SECOND)


# ---- sync_runs bookkeeping (rolled back)


async def test_the_latest_good_watermark_is_found(session: AsyncSession) -> None:
    first = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
    second = datetime(2026, 10, 17, 11, 0, tzinfo=UTC)
    for watermark in (first, second):
        run_id, _ = await repo.start_sync_run(session, KIND)
        await repo.finish_sync_run(session, run_id, "ok", {"seen": 0}, None, watermark)
    assert await repo.last_ok_watermark(session, KIND) == second  # the newest run wins


async def test_a_failed_run_never_gives_a_watermark(session: AsyncSession) -> None:
    good = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
    ok_id, _ = await repo.start_sync_run(session, KIND)
    await repo.finish_sync_run(session, ok_id, "ok", {}, None, good)
    bad_id, _ = await repo.start_sync_run(session, KIND)
    await repo.finish_sync_run(session, bad_id, "failed", {}, "Shopify is down", None)
    assert await repo.last_ok_watermark(session, KIND) == good


async def test_there_is_no_watermark_before_the_first_good_run(session: AsyncSession) -> None:
    assert await repo.last_ok_watermark(session, KIND) is None


# ---- run_job: recorded in sync_runs (committed, then cleaned up)


async def get_run(maker: async_sessionmaker[AsyncSession], run_id: int) -> SyncRun:
    async with maker() as db:
        return (await db.execute(select(SyncRun).where(SyncRun.id == run_id))).scalar_one()


def use_test_job(
    monkeypatch: pytest.MonkeyPatch, job: Any, seen_since: list[datetime] | None = None
) -> None:
    """Replace the jobs by one made-up 'test-delta' job. It records the `since` it was given."""

    async def wrapper(maker: async_sessionmaker[AsyncSession], since: datetime) -> dict[str, Any]:
        if seen_since is not None:
            seen_since.append(since)
        return await job()

    monkeypatch.setitem(reconcile.JOBS, KIND, wrapper)
    monkeypatch.setattr(reconcile, "WATERMARK_KINDS", frozenset({KIND}))


async def test_a_good_run_is_recorded_with_its_counts_and_watermark(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def job() -> dict[str, Any]:
        return {"seen": 3}

    seen: list[datetime] = []
    use_test_job(monkeypatch, job, seen)

    result = await reconcile.run_job(sessionmaker, KIND)

    assert (result.status, result.counts, result.error) == ("ok", {"seen": 3}, None)
    row = await get_run(sessionmaker, result.run_id)
    assert row.status == "ok"
    assert row.counts == {"seen": 3}
    assert row.finished_at is not None
    assert row.watermark == row.started_at  # the next run continues from here
    assert seen == [row.started_at - reconcile.FIRST_LOOKBACK]  # no earlier run: look one day back


async def test_the_next_run_starts_from_the_last_watermark_with_an_overlap(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def job() -> dict[str, Any]:
        return {}

    seen: list[datetime] = []
    use_test_job(monkeypatch, job, seen)

    first = await reconcile.run_job(sessionmaker, KIND)
    await reconcile.run_job(sessionmaker, KIND)

    first_row = await get_run(sessionmaker, first.run_id)
    assert seen[1] == first_row.started_at - reconcile.OVERLAP


async def test_a_failed_run_is_recorded_and_the_next_run_does_not_skip_anything(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes: list[Exception | None] = [None, ShopifyError("Shopify is down (test)"), None]

    async def job() -> dict[str, Any]:
        problem = outcomes.pop(0)
        if problem is not None:
            raise problem
        return {}

    seen: list[datetime] = []
    use_test_job(monkeypatch, job, seen)

    good = await reconcile.run_job(sessionmaker, KIND)
    bad = await reconcile.run_job(sessionmaker, KIND)
    await reconcile.run_job(sessionmaker, KIND)

    assert (bad.status, bad.error) == ("failed", "Shopify is down (test)")
    bad_row = await get_run(sessionmaker, bad.run_id)
    assert bad_row.status == "failed"
    assert bad_row.watermark is None
    assert bad_row.finished_at is not None
    good_row = await get_run(sessionmaker, good.run_id)
    # the run after the failure starts from the last GOOD run, not from the failed one
    assert seen[2] == good_row.started_at - reconcile.OVERLAP


async def test_an_unexpected_error_is_recorded_by_its_type(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def job() -> dict[str, Any]:
        raise RuntimeError("something odd")

    use_test_job(monkeypatch, job)

    result = await reconcile.run_job(sessionmaker, KIND)

    assert (result.status, result.error) == ("failed", "RuntimeError")
    assert (await get_run(sessionmaker, result.run_id)).error == "RuntimeError"


def test_the_overlap_is_shorter_than_a_day() -> None:
    assert timedelta(0) < reconcile.OVERLAP < reconcile.FIRST_LOOKBACK
