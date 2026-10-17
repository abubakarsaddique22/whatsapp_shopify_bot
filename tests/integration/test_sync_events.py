"""Shopify webhook endpoint tests (T16 for Shopify) on the real Postgres, through the real app.

Each test works inside a transaction that is rolled back at the end, so no row stays in the
database. Needs: docker compose up -d db, and uv run alembic upgrade head (the sync tables).
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api import shopify_webhook
from app.core.security import sign_shopify
from app.core.settings import Settings, get_settings
from app.db.session import get_session
from app.db.tables import SyncEvent
from app.main import app

SECRET = "test-shopify-secret"
SHOP = "test-shop.myshopify.com"
TEST_SETTINGS = Settings(
    shopify_client_secret=SecretStr(SECRET),
    shopify_shop_domain=SHOP,
)


# ---- helpers


def new_event_id() -> str:
    return f"test-event-{uuid.uuid4().hex[:12]}"


def product_body(number: int = 42) -> dict[str, Any]:
    return {
        "id": number,
        "admin_graphql_api_id": f"gid://shopify/Product/{number}",
        "title": "Test Watch",
        "body_html": "<p>a long description that must not be stored</p>",
        "updated_at": "2026-10-16T18:00:00+00:00",
    }


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
    monkeypatch.setattr(shopify_webhook, "get_settings", lambda: TEST_SETTINGS)

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
    *,
    event_id: str | None = None,
    topic: str = "products/update",
    shop: str = SHOP,
    secret: str | None = SECRET,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST a webhook. secret=None sends no signature; event_id=None sends no event id."""
    raw = json.dumps(body).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Topic": topic,
        "X-Shopify-Shop-Domain": shop,
        "X-Shopify-Triggered-At": "2026-10-16T18:00:27.877041743Z",
    }
    if event_id is not None:
        headers["X-Shopify-Event-Id"] = event_id
    if secret is not None:
        headers["X-Shopify-Hmac-Sha256"] = sign_shopify(raw, secret)
    headers.update(extra_headers or {})
    return await client.post("/webhook/shopify", content=raw, headers=headers)


async def events(session: AsyncSession, *webhook_ids: str) -> list[Any]:
    result = await session.execute(
        select(
            SyncEvent.webhook_id,
            SyncEvent.topic,
            SyncEvent.shop_domain,
            SyncEvent.resource_id,
            SyncEvent.status,
            SyncEvent.attempts,
            SyncEvent.payload,
            SyncEvent.triggered_at,
        )
        .where(SyncEvent.webhook_id.in_(webhook_ids))
        .order_by(SyncEvent.id)
    )
    return list(result.all())


# ---- signature


async def test_bad_signature_is_401_and_nothing_is_stored(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    event_id = new_event_id()
    response = await post(client, product_body(), event_id=event_id, secret="wrong-secret")
    assert response.status_code == 401
    assert await events(session, event_id) == []


async def test_missing_signature_is_401(client: httpx.AsyncClient, session: AsyncSession) -> None:
    event_id = new_event_id()
    response = await post(client, product_body(), event_id=event_id, secret=None)
    assert response.status_code == 401
    assert await events(session, event_id) == []


async def test_signature_of_a_different_body_is_401(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """The signature is computed on the exact bytes: a changed body must not pass."""
    event_id = new_event_id()
    raw = json.dumps(product_body()).encode()
    headers = {
        "X-Shopify-Topic": "products/update",
        "X-Shopify-Shop-Domain": SHOP,
        "X-Shopify-Event-Id": event_id,
        "X-Shopify-Hmac-Sha256": sign_shopify(raw, SECRET),
    }
    tampered = json.dumps(product_body(number=43)).encode()
    response = await client.post("/webhook/shopify", content=tampered, headers=headers)
    assert response.status_code == 401
    assert await events(session, event_id) == []


# ---- storing


async def test_product_update_is_stored_as_pending(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    event_id = new_event_id()
    response = await post(client, product_body(42), event_id=event_id)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    rows = await events(session, event_id)
    assert len(rows) == 1
    row = rows[0]
    assert (row.topic, row.shop_domain, row.status, row.attempts) == (
        "products/update",
        SHOP,
        "pending",
        0,
    )
    assert row.resource_id == "gid://shopify/Product/42"
    assert row.triggered_at is not None


async def test_only_small_fields_of_the_body_are_stored(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    event_id = new_event_id()
    await post(client, product_body(42), event_id=event_id)
    row = (await events(session, event_id))[0]
    assert "body_html" not in row.payload
    assert "title" not in row.payload
    assert row.payload["id"] == 42


async def test_inventory_event_is_stored_for_the_inventory_item(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    event_id = new_event_id()
    body = {"inventory_item_id": 77, "location_id": 5, "available": 3}
    response = await post(client, body, event_id=event_id, topic="inventory_levels/update")
    assert response.status_code == 200
    row = (await events(session, event_id))[0]
    assert row.resource_id == "gid://shopify/InventoryItem/77"
    assert row.payload["available"] == 3


async def test_product_delete_is_stored(client: httpx.AsyncClient, session: AsyncSession) -> None:
    event_id = new_event_id()
    response = await post(client, {"id": 42}, event_id=event_id, topic="products/delete")
    assert response.status_code == 200
    row = (await events(session, event_id))[0]
    assert (row.topic, row.resource_id) == ("products/delete", "gid://shopify/Product/42")


# ---- T16: the same delivery twice


async def test_t16_second_delivery_of_the_same_event_is_ignored(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    event_id = new_event_id()
    first = await post(client, product_body(), event_id=event_id)
    second = await post(client, product_body(), event_id=event_id)  # Shopify delivered it again
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert len(await events(session, event_id)) == 1


async def test_two_different_events_give_two_rows(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    first_id, second_id = new_event_id(), new_event_id()
    await post(client, product_body(), event_id=first_id)
    await post(client, product_body(), event_id=second_id)
    assert len(await events(session, first_id, second_id)) == 2


async def test_webhook_id_is_the_fallback_when_event_id_is_missing(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    webhook_id = new_event_id()
    response = await post(
        client,
        product_body(),
        event_id=None,
        extra_headers={"X-Shopify-Webhook-Id": webhook_id},
    )
    assert response.status_code == 200
    assert len(await events(session, webhook_id)) == 1


# ---- things that are answered 200 but not stored


async def test_delivery_without_any_id_is_ignored(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    response = await post(client, product_body(), event_id=None)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


async def test_unsupported_topic_is_ignored_and_not_stored(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """The bot is read-only: order or customer topics must never reach the database."""
    event_id = new_event_id()
    response = await post(client, {"id": 1}, event_id=event_id, topic="orders/create")
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert await events(session, event_id) == []


async def test_event_from_another_shop_is_ignored(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    event_id = new_event_id()
    response = await post(client, product_body(), event_id=event_id, shop="other.myshopify.com")
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert await events(session, event_id) == []


async def test_body_that_is_not_json_is_ignored_with_200(client: httpx.AsyncClient) -> None:
    raw = b"this is not json"
    headers = {
        "X-Shopify-Topic": "products/update",
        "X-Shopify-Shop-Domain": SHOP,
        "X-Shopify-Event-Id": new_event_id(),
        "X-Shopify-Hmac-Sha256": sign_shopify(raw, SECRET),
    }
    response = await client.post("/webhook/shopify", content=raw, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
