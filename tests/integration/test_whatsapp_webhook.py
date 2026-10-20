"""Webhook tests (T16 foundation) on the real Postgres, through the real FastAPI app.

Each test works inside a transaction that is rolled back at the end, so no row stays in the
database. Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api import whatsapp_webhook
from app.core.security import sign_whatsapp
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.db.session import get_session
from app.db.tables import Conversation, WhatsAppInbox, WhatsAppOutbox
from app.main import app

SECRET = "test-app-secret"
VERIFY = "test-verify-token"
WA_ID = "920000000001"
TEST_SETTINGS = Settings(
    whatsapp_app_secret=SecretStr(SECRET),
    whatsapp_verify_token=SecretStr(VERIFY),
    phone_hash_pepper=SecretStr("test-pepper"),
)


# ---- helpers


def new_wamid() -> str:
    return f"wamid.test-{uuid.uuid4().hex[:12]}"


def text_message(wamid: str, body: str = "hello", wa_id: str = WA_ID) -> dict[str, Any]:
    return {
        "from": wa_id,
        "id": wamid,
        "timestamp": "1760169600",
        "type": "text",
        "text": {"body": body},
    }


def image_message(wamid: str) -> dict[str, Any]:
    return {
        "from": WA_ID,
        "id": wamid,
        "timestamp": "1760169600",
        "type": "image",
        "image": {"id": "media-1", "mime_type": "image/jpeg"},
    }


def payload(
    messages: list[dict[str, Any]] | None = None,
    statuses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {"messaging_product": "whatsapp"}
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": value}]}]}


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
    monkeypatch.setattr(whatsapp_webhook, "get_settings", lambda: TEST_SETTINGS)

    async def use_test_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = use_test_session
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()


async def post(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    secret: str | None = SECRET,
) -> httpx.Response:
    """POST a payload. secret=None sends no signature at all."""
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers["X-Hub-Signature-256"] = sign_whatsapp(raw, secret)
    return await client.post("/webhook/whatsapp", content=raw, headers=headers)


async def inbox_rows(session: AsyncSession, *wamids: str) -> list[Any]:
    result = await session.execute(
        select(
            WhatsAppInbox.wamid,
            WhatsAppInbox.msg_type,
            WhatsAppInbox.text,
            WhatsAppInbox.status,
            WhatsAppInbox.conversation_id,
        )
        .where(WhatsAppInbox.wamid.in_(wamids))
        .order_by(WhatsAppInbox.id)
    )
    return list(result.all())


async def count_rows(session: AsyncSession, table: type) -> int:
    result = await session.execute(select(func.count()).select_from(table))
    return result.scalar_one()


# ---- GET handshake


async def test_handshake_returns_the_challenge(client: httpx.AsyncClient) -> None:
    params = {"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "12345"}
    response = await client.get("/webhook/whatsapp", params=params)
    assert response.status_code == 200
    assert response.text == "12345"


async def test_handshake_with_wrong_token_is_403(client: httpx.AsyncClient) -> None:
    params = {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"}
    response = await client.get("/webhook/whatsapp", params=params)
    assert response.status_code == 403


async def test_handshake_without_parameters_is_403(client: httpx.AsyncClient) -> None:
    assert (await client.get("/webhook/whatsapp")).status_code == 403


# ---- POST signature


async def test_bad_signature_is_401_and_nothing_is_stored(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    wamid = new_wamid()
    response = await post(client, payload([text_message(wamid)]), secret="wrong-secret")
    assert response.status_code == 401
    assert await inbox_rows(session, wamid) == []


async def test_missing_signature_is_401(client: httpx.AsyncClient, session: AsyncSession) -> None:
    wamid = new_wamid()
    response = await post(client, payload([text_message(wamid)]), secret=None)
    assert response.status_code == 401
    assert await inbox_rows(session, wamid) == []


# ---- POST storing


async def test_text_message_is_stored_as_pending(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    wamid = new_wamid()
    response = await post(client, payload([text_message(wamid, "hello")]))
    assert response.status_code == 200
    rows = await inbox_rows(session, wamid)
    assert len(rows) == 1
    assert (rows[0].msg_type, rows[0].text, rows[0].status) == ("text", "hello", "pending")


async def test_phone_number_is_hashed_in_the_conversation(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await post(client, payload([text_message(new_wamid())]))
    result = await session.execute(
        select(Conversation.wa_id, Conversation.wa_id_hash).where(Conversation.wa_id == WA_ID)
    )
    conversation = result.one()
    assert conversation.wa_id == WA_ID  # needed to send a reply
    assert conversation.wa_id_hash != WA_ID
    assert len(conversation.wa_id_hash) == 16


async def test_t16_duplicate_wamid_gives_one_row(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    wamid = new_wamid()
    body = payload([text_message(wamid)])
    assert (await post(client, body)).status_code == 200
    assert (await post(client, body)).status_code == 200  # Meta delivered it again
    assert len(await inbox_rows(session, wamid)) == 1


async def test_multi_message_payload_stores_each_message_once(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    first, second = new_wamid(), new_wamid()
    messages = [text_message(first, "one"), text_message(second, "two")]
    response = await post(client, payload(messages))
    assert response.status_code == 200
    rows = await inbox_rows(session, first, second)
    assert [r.text for r in rows] == ["one", "two"]
    assert rows[0].conversation_id == rows[1].conversation_id  # same phone number


async def test_unsupported_type_is_stored_for_the_worker(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    wamid = new_wamid()
    assert (await post(client, payload([image_message(wamid)]))).status_code == 200
    rows = await inbox_rows(session, wamid)
    assert (rows[0].msg_type, rows[0].status) == ("image", "pending")


async def test_status_only_payload_stores_no_message(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    status = {"id": new_wamid(), "status": "delivered", "recipient_id": WA_ID}
    before = await count_rows(session, Conversation)
    inbox_before = await count_rows(session, WhatsAppInbox)
    assert (await post(client, payload(statuses=[status]))).status_code == 200
    assert await count_rows(session, Conversation) == before  # no message, no new conversation
    assert await count_rows(session, WhatsAppInbox) == inbox_before


async def test_failed_status_marks_the_outbox_row(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    out_wamid = new_wamid()
    conversation_id = await repo.get_or_create_conversation(session, WA_ID, "testhash")
    session.add(
        WhatsAppOutbox(
            conversation_id=conversation_id, seq=0, body="Received", status="sent", wamid=out_wamid
        )
    )
    await session.flush()

    status = {
        "id": out_wamid,
        "status": "failed",
        "recipient_id": WA_ID,
        "errors": [{"code": 131031}],
    }
    assert (await post(client, payload(statuses=[status]))).status_code == 200

    result = await session.execute(
        select(WhatsAppOutbox.status, WhatsAppOutbox.last_error).where(
            WhatsAppOutbox.wamid == out_wamid
        )
    )
    row = result.one()
    assert row.status == "failed"
    assert "131031" in row.last_error


async def test_an_owner_reply_from_the_business_app_silences_the_bot_for_that_customer(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """P8: Meta sends the owner's WhatsApp Business app message as smb_message_echoes."""
    echo_wamid = new_wamid()
    body = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "smb_message_echoes",
                        "value": {
                            "messaging_product": "whatsapp",
                            "message_echoes": [
                                {
                                    "from": "923000000000",
                                    "to": WA_ID,
                                    "id": echo_wamid,
                                    "timestamp": "1760169600",
                                    "type": "text",
                                    "text": {"body": "Ji, main dekhta hoon"},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }
    assert (await post(client, body)).status_code == 200

    assert await inbox_rows(session, echo_wamid) == []  # not a customer message
    result = await session.execute(select(Conversation.id).where(Conversation.wa_id == WA_ID))
    state = await repo.load_state(session, result.scalar_one())
    assert state.owner_active_until is not None


async def test_body_that_is_not_json_is_ignored_with_200(client: httpx.AsyncClient) -> None:
    raw = b"this is not json"
    headers = {"X-Hub-Signature-256": sign_whatsapp(raw, SECRET)}
    response = await client.post("/webhook/whatsapp", content=raw, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
