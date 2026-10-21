"""The team's bot ON / OFF page (/team), on the real Postgres, every test rolled back.

A small FastAPI app holds only the team router and talks to the test's own session.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api import team
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.db.session import get_session
from app.db.tables import SecurityEvent
from app.main import create_app
from app.owner.presence import bot_is_off, set_bot

PASSWORD = "team-password-123"


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
async def client(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    on = Settings(_env_file=None, team_password=SecretStr(PASSWORD), app_env="local")
    monkeypatch.setattr(team, "get_settings", lambda: on)
    monkeypatch.setattr(team, "WRONG_PASSWORD_DELAY", 0)
    team._failures.clear()
    await set_bot(session, on=True)

    app = FastAPI()
    app.include_router(team.router)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://team") as http:
        yield http


async def login(client: httpx.AsyncClient, password: str = PASSWORD) -> httpx.Response:
    return await client.post("/team/login", data={"password": password})


async def test_without_login_the_page_asks_for_the_password(client: httpx.AsyncClient) -> None:
    page = await client.get("/team")
    assert page.status_code == 200
    assert 'name="password"' in page.text
    assert 'name="state"' not in page.text  # no button before the login


async def test_a_wrong_password_shows_an_error_and_gives_no_cookie(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    response = await login(client, "wrong-password")
    assert "Password ghalat hai" in response.text
    assert team.COOKIE not in response.cookies
    assert 'name="state"' not in (await client.get("/team")).text


async def test_after_login_the_page_shows_the_state_and_switches_the_bot(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    response = await login(client)
    assert response.status_code == 303
    assert "Bot OFF karein" in (await client.get("/team")).text  # it is ON now

    await client.post("/team/bot", data={"state": "off"})
    assert await bot_is_off(session) is True
    page = (await client.get("/team")).text
    assert "Bot OFF" in page and "Bot ON karein" in page and "3 minute" in page

    await client.post("/team/bot", data={"state": "on"})
    assert await bot_is_off(session) is False

    result = await session.execute(
        select(SecurityEvent.detail)
        .where(SecurityEvent.kind == "team_switch")
        .order_by(SecurityEvent.id)
    )
    assert [row["bot_on"] for row in result.scalars()] == [False, True]  # both switches audited


async def test_the_switch_needs_a_login(client: httpx.AsyncClient, session: AsyncSession) -> None:
    await client.post("/team/bot", data={"state": "off"})
    assert await bot_is_off(session) is False


async def test_a_forged_cookie_does_not_work(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    client.cookies.set(team.COOKIE, "0" * 64)
    await client.post("/team/bot", data={"state": "off"})
    assert await bot_is_off(session) is False


async def test_an_automatic_switch_on_is_shown_to_the_team(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await login(client)
    await set_bot(session, on=True, automatic=True)
    assert "khud ON" in (await client.get("/team")).text


async def test_too_many_wrong_passwords_lock_the_login(client: httpx.AsyncClient) -> None:
    for _ in range(team.MAX_FAILURES):
        await login(client, "wrong-password")
    response = await login(client)  # even the right password is refused for a while
    assert "Bohat dafa ghalat password" in response.text
    assert team.COOKIE not in response.cookies


async def test_logout_ends_the_session(client: httpx.AsyncClient) -> None:
    await login(client)
    await client.post("/team/logout")
    assert 'name="password"' in (await client.get("/team")).text


async def test_the_kill_switch_is_shown_on_the_page(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await login(client)
    await repo.set_flag(session, repo.KILL_SWITCH_FLAG, True)
    assert "Kill switch ON" in (await client.get("/team")).text


def test_without_a_password_the_page_does_not_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main

    monkeypatch.setattr(main, "get_settings", lambda: Settings(_env_file=None))
    assert "/team" not in create_app().openapi()["paths"]

    with_password = Settings(_env_file=None, team_password=SecretStr(PASSWORD))
    monkeypatch.setattr(main, "get_settings", lambda: with_password)
    assert "/team" in create_app().openapi()["paths"]
