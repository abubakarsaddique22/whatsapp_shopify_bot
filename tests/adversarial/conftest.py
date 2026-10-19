"""Fixtures of the adversarial tests."""

import io
import logging
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api import admin
from app.core.logging import JsonFormatter, RedactionFilter
from app.core.settings import Settings, get_settings
from app.db.session import get_session
from app.db.tables import Handoff, WhatsAppInbox, WhatsAppOutbox
from tests.adversarial.harness import Bot, install_fakes
from tests.adversarial.worker_kit import Chat

ADMIN_KEY = "k" * 32
ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> Bot:
    """The real graph with fake tools, a FakeLLM and fake secrets (see harness.py)."""
    return install_fakes(monkeypatch)


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
            await session.execute(delete(Handoff))  # old handoffs must not use up a notify pass
            yield session
            await session.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def chat(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Chat:
    """One customer talking to the real worker (see worker_kit.py)."""
    return Chat(db, monkeypatch)


@pytest.fixture
async def admin_client(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """The admin API (switched on, key ADMIN_KEY) talking to the test's own session."""
    on = Settings(_env_file=None, admin_endpoints_enabled=True, admin_api_key=ADMIN_KEY)
    monkeypatch.setattr(admin, "get_settings", lambda: on)
    app = FastAPI()
    app.include_router(admin.router)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://admin") as http:
        yield http


@pytest.fixture
def log_text() -> Iterator[Callable[[], str]]:
    """Everything the app logs while the test runs, formatted like production: JSON lines through
    the redaction filter, INFO level. Call it to get the text so far."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield stream.getvalue
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
