"""Fixtures of the failure drills (Phase 12, Step 5). See src/app/evals/drills.py.

Every drill runs the REAL worker functions (`worker.process_inbox`, `worker.send_outbox`) on the
real Postgres. Those functions open their own sessions from a sessionmaker and commit or roll back
on their own, so the drills hand them a `SavepointMaker`: every `async with sessionmaker()` gets
the test's session inside a SAVEPOINT. A commit releases the savepoint, a rollback really undoes
that pass (which is what a crash test needs), and at the end the whole test is rolled back, so no
row stays in the dev database.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction, create_async_engine

from app.core.settings import get_settings
from app.db import repositories as repo
from app.db.tables import Handoff, WhatsAppInbox, WhatsAppOutbox
from tests.adversarial.worker_kit import Chat


class SavepointSession:
    """The test's session for one `async with sessionmaker()` block, inside a SAVEPOINT."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._savepoint: AsyncSessionTransaction | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def begin(self) -> None:
        self._savepoint = await self._session.begin_nested()

    async def commit(self) -> None:
        if self._savepoint is not None and self._savepoint.is_active:
            await self._savepoint.commit()
        await self.begin()

    async def rollback(self) -> None:
        if self._savepoint is not None and self._savepoint.is_active:
            await self._savepoint.rollback()
        await self.begin()

    async def close(self) -> None:
        """Leaving the block without a commit discards the work, like a real session."""
        if self._savepoint is not None and self._savepoint.is_active:
            await self._savepoint.rollback()
        self._savepoint = None


class _Block:
    def __init__(self, session: AsyncSession) -> None:
        self._wrapped = SavepointSession(session)

    async def __aenter__(self) -> SavepointSession:
        await self._wrapped.begin()
        return self._wrapped

    async def __aexit__(self, *exc: object) -> None:
        await self._wrapped.close()


class SavepointMaker:
    """Stands in for `async_sessionmaker` in worker.process_inbox / worker.send_outbox."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> _Block:
        return _Block(self._session)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """A real database session; everything is rolled back at the end of the test."""
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            session = AsyncSession(bind=conn, expire_on_commit=False)
            # the worker takes the oldest pending row of ANY customer: start from an empty queue
            await session.execute(delete(WhatsAppOutbox))
            await session.execute(delete(WhatsAppInbox))
            await session.execute(delete(Handoff))
            await repo.set_flag(session, repo.KILL_SWITCH_FLAG, False)
            yield session
            await session.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def chat(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Chat:
    """One customer and the real worker (tests/adversarial/worker_kit.py)."""
    return Chat(db, monkeypatch, wa_id="920000000055", wa_hash="testhash-drill")


@pytest.fixture
def maker(db: AsyncSession) -> Any:
    return SavepointMaker(db)
