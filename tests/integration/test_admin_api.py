"""Phase 11, step 8: the admin endpoints and the kill switch the worker reads from the database.

Real Postgres, every test rolled back. The API is a small FastAPI app that holds only the admin
router, talking to the test's own session (nothing is committed for real).
Needs: docker compose up -d db, and uv run alembic upgrade head (adds runtime_flags).
"""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import worker
from app.api import admin
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.db.session import get_session
from app.db.tables import SecurityEvent, WhatsAppInbox, WhatsAppOutbox
from app.handoff import service as handoff_service
from app.integrations.llm.fake_llm import FakeLLM
from app.integrations.whatsapp.payloads import InboundMessage

KEY = "k" * 32
HEADERS = {"X-Admin-Key": KEY}
WA_ID = "920000000012"
HASH = "testhash12"


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


@pytest.fixture
async def client(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    on = Settings(admin_endpoints_enabled=True, admin_api_key=KEY)
    monkeypatch.setattr(admin, "get_settings", lambda: on)
    monkeypatch.setattr(worker, "get_settings", lambda: Settings(debug_reply=False))
    monkeypatch.setattr(worker, "get_chat_model", lambda role="router": FakeLLM([]))

    app = FastAPI()
    app.include_router(admin.router)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://admin") as http:
        yield http


async def conversation_id(session: AsyncSession) -> int:
    return await repo.get_or_create_conversation(session, WA_ID, HASH)


async def receive_and_process(session: AsyncSession, text: str = "hmm") -> str:
    msg = InboundMessage(
        wamid=f"wamid.test-{uuid.uuid4().hex[:12]}", wa_id=WA_ID, msg_type="text", text=text
    )
    inbox_id = await repo.add_inbox(session, await conversation_id(session), HASH, msg)
    assert inbox_id is not None
    inbox = await repo.claim_inbox(session)
    assert inbox is not None
    await worker.handle_inbox(session, inbox)
    result = await session.execute(select(WhatsAppInbox.status).where(WhatsAppInbox.id == inbox_id))
    return result.scalar_one()


# ---- access


async def test_endpoints_answer_404_while_they_are_switched_off(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = Settings(admin_endpoints_enabled=False)
    monkeypatch.setattr(admin, "get_settings", lambda: off)
    assert (await client.get("/admin/status", headers=HEADERS)).status_code == 404


async def test_a_missing_or_wrong_key_is_refused(client: httpx.AsyncClient) -> None:
    assert (await client.get("/admin/status")).status_code == 401
    wrong = {"X-Admin-Key": "x" * 32}
    assert (await client.get("/admin/status", headers=wrong)).status_code == 401
    attempt = await client.post("/admin/kill-switch", json={"on": True}, headers=wrong)
    assert attempt.status_code == 401


# ---- kill switch


async def test_the_kill_switch_stops_the_worker_and_can_be_turned_off(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    on = await client.post("/admin/kill-switch", json={"on": True}, headers=HEADERS)
    assert on.status_code == 200 and on.json()["active"] is True

    assert await receive_and_process(session) == "ignored"  # no reply, no cost
    sent = await session.execute(select(WhatsAppOutbox.id))
    assert sent.all() == []

    off = await client.post("/admin/kill-switch", json={"on": False}, headers=HEADERS)
    assert off.json()["active"] is False
    assert await receive_and_process(session) == "done"  # the bot answers again


# ---- pause and resume


async def test_pause_then_resume_one_conversation(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    cid = await conversation_id(session)

    url = f"/admin/conversations/{cid}"

    paused = await client.post(f"{url}/pause", json={"hours": 2}, headers=HEADERS)
    assert paused.status_code == 200
    assert handoff_service.is_paused(await repo.load_state(session, cid))

    resumed = await client.post(f"{url}/resume", headers=HEADERS)
    assert resumed.status_code == 200 and resumed.json()["was_paused"] is True
    assert not handoff_service.is_paused(await repo.load_state(session, cid))


async def test_pause_without_a_body_uses_the_default_hours(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    cid = await conversation_id(session)
    response = await client.post(f"/admin/conversations/{cid}/pause", headers=HEADERS)
    assert response.status_code == 200
    assert handoff_service.is_paused(await repo.load_state(session, cid))


async def test_an_unknown_conversation_is_a_404(client: httpx.AsyncClient) -> None:
    for action in ("pause", "resume"):
        response = await client.post(f"/admin/conversations/999999/{action}", headers=HEADERS)
        assert response.status_code == 404


# ---- audit and lists


async def test_every_change_leaves_an_audit_row_without_customer_data(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    cid = await conversation_id(session)
    await client.post("/admin/kill-switch", json={"on": True}, headers=HEADERS)
    await client.post(f"/admin/conversations/{cid}/pause", headers=HEADERS)

    rows = await session.execute(
        select(SecurityEvent.detail).where(SecurityEvent.kind == "admin_action")
    )
    details = [row[0] for row in rows.all()]
    assert {"action": "kill_switch", "on": True} in details
    assert {"action": "pause", "conversation_id": cid} in details
    assert WA_ID not in str(details)


async def test_status_and_handoff_list(client: httpx.AsyncClient, session: AsyncSession) -> None:
    cid = await conversation_id(session)
    conv = await repo.load_state(session, cid)
    await handoff_service.create_handoff(session, cid, conv, "human_request")

    status = (await client.get("/admin/status", headers=HEADERS)).json()
    assert status["kill_switch"]["active"] is False and status["open_handoffs"] >= 1

    listed = (await client.get("/admin/handoffs", headers=HEADERS)).json()
    mine = [row for row in listed if row["conversation_id"] == cid]
    assert len(mine) == 1 and mine[0]["reason"] == "human_request"
