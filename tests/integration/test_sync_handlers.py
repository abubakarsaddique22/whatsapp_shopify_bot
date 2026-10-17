"""Sync handler and worker-pass tests (Phase 10, step 4) on the real Postgres.

Each test runs inside a transaction that is rolled back at the end, so no row stays in the
database. A small FakeShopify decides what "Shopify" answers (product found, gone, down); the
database, the mirror code and the claim / retry code are the real ones.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.catalog import stock as stock_mod
from app.catalog.importer import image_rows, product_row, variant_rows
from app.core.errors import ShopifyError, ValidationFailed
from app.core.settings import get_settings
from app.db import repositories as repo
from app.db.tables import SyncEvent, VariantMirror
from app.domain.catalog import Money, Product, StockState
from app.sync import handlers
from app.sync.webhooks import ShopifyDelivery
from tests.factories import make_product, make_variant

PRODUCT_ID = "gid://shopify/Product/9990001"
VARIANT_ID = "gid://shopify/ProductVariant/99900011"  # the variant make_product() builds
ITEM_ID = "gid://shopify/InventoryItem/9990099"


# ---- fixtures and helpers


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            # Real events that wait in the dev database must not be taken by these tests
            # (this change is rolled back too).
            await db.execute(
                update(SyncEvent).where(SyncEvent.status == "pending").values(status="ignored")
            )
            yield db
            await db.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


class FakeShopify:
    """What 'Shopify' answers to the two calls the handlers make."""

    def __init__(self) -> None:
        self.products: dict[str, Product] = {}  # a product missing here is gone or hidden
        self.items: dict[str, str] = {}  # inventory item id -> product id
        self.down = False
        self.bad_ids = False

    def _check(self) -> None:
        if self.down:
            raise ShopifyError("Shopify is down (test)")
        if self.bad_ids:
            raise ValidationFailed("Not a valid Shopify id")

    async def get_product(self, product_id: str) -> Product | None:
        self._check()
        return self.products.get(product_id)

    async def product_id_of_inventory_item(self, item_id: str) -> str | None:
        self._check()
        return self.items.get(item_id)


@pytest.fixture
def shopify(monkeypatch: pytest.MonkeyPatch) -> FakeShopify:
    fake = FakeShopify()
    monkeypatch.setattr(handlers.gateway, "get_product", fake.get_product)
    monkeypatch.setattr(
        handlers.gateway, "product_id_of_inventory_item", fake.product_id_of_inventory_item
    )
    return fake


def event(topic: str, resource_id: str | None) -> SyncEvent:
    """An event that is not stored: handle_event only reads its topic and resource id."""
    return SyncEvent(topic=topic, resource_id=resource_id, webhook_id="not-stored")


async def queue(
    session: AsyncSession, topic: str = "products/update", resource_id: str | None = PRODUCT_ID
) -> int:
    """Store a pending event the way the webhook endpoint does."""
    delivery = ShopifyDelivery(
        webhook_id=f"test-{uuid.uuid4().hex[:12]}",
        topic=topic,
        shop_domain="test-shop.myshopify.com",
        resource_id=resource_id,
        triggered_at=None,
        payload={},
    )
    event_id = await repo.add_sync_event(session, delivery)
    assert event_id is not None
    return event_id


async def row(session: AsyncSession, event_id: int) -> SyncEvent:
    result = await session.execute(
        select(SyncEvent).where(SyncEvent.id == event_id).execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def visible(session: AsyncSession, product_id: str = PRODUCT_ID) -> bool:
    return await repo.product_basics(session, product_id) is not None


async def add_product(session: AsyncSession, shopify: FakeShopify, product: Product) -> None:
    """Put a product into the mirror through the real create handler."""
    shopify.products[product.id] = product
    assert await handlers.handle_event(session, event("products/create", product.id)) == "added"


# ---- products/create and products/update


async def test_create_event_adds_the_product_to_the_mirror(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    shopify.products[PRODUCT_ID] = make_product()
    outcome = await handlers.handle_event(session, event("products/create", PRODUCT_ID))
    assert outcome == "added"
    assert await visible(session)


async def test_update_event_changes_the_title_in_the_mirror(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    await add_product(session, shopify, make_product())
    shopify.products[PRODUCT_ID] = make_product(title="Renamed Watch")
    outcome = await handlers.handle_event(session, event("products/update", PRODUCT_ID))
    assert outcome == "updated"
    basics = await repo.product_basics(session, PRODUCT_ID)
    assert basics is not None
    assert basics.title == "Renamed Watch"


async def test_update_event_brings_a_new_price_into_the_mirror(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    await add_product(session, shopify, make_product())
    cheaper = make_variant(
        id=VARIANT_ID,
        product_id=PRODUCT_ID,
        price=Money(amount=Decimal("9900"), currency="PKR"),
    )
    shopify.products[PRODUCT_ID] = make_product(variants=[cheaper])
    assert await handlers.handle_event(session, event("products/update", PRODUCT_ID)) == "updated"
    price = (
        await session.execute(
            select(VariantMirror.price_amount).where(VariantMirror.id == VARIANT_ID)
        )
    ).scalar_one()
    assert price == Decimal("9900")


async def test_the_same_event_twice_changes_nothing(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    await add_product(session, shopify, make_product())
    outcome = await handlers.handle_event(session, event("products/update", PRODUCT_ID))
    assert outcome == "unchanged"


async def test_a_new_stock_number_alone_does_not_rewrite_the_mirror(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    """Stock counts change all day and are always read live, so they are not part of the hash."""
    await add_product(session, shopify, make_product())
    fewer = make_variant(id=VARIANT_ID, product_id=PRODUCT_ID, inventory_quantity=3)
    shopify.products[PRODUCT_ID] = make_product(variants=[fewer])
    outcome = await handlers.handle_event(session, event("products/update", PRODUCT_ID))
    assert outcome == "unchanged"


async def test_a_product_that_is_no_longer_visible_is_hidden(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    await add_product(session, shopify, make_product())
    del shopify.products[PRODUCT_ID]  # now a draft: Shopify no longer returns it
    outcome = await handlers.handle_event(session, event("products/update", PRODUCT_ID))
    assert outcome == "hidden"
    assert not await visible(session)


async def test_a_hidden_product_that_is_visible_again_is_shown_again(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    product = make_product()
    await add_product(session, shopify, product)
    await repo.hide_product(session, PRODUCT_ID)
    assert not await visible(session)
    outcome = await handlers.handle_event(session, event("products/update", PRODUCT_ID))
    assert outcome == "updated"
    assert await visible(session)


# ---- products/delete


async def test_delete_event_hides_the_product(session: AsyncSession, shopify: FakeShopify) -> None:
    await add_product(session, shopify, make_product())
    outcome = await handlers.handle_event(session, event("products/delete", PRODUCT_ID))
    assert outcome == "hidden"
    assert not await visible(session)
    assert await repo.product_state(session, PRODUCT_ID) is not None  # hidden, not deleted


async def test_delete_event_for_an_unknown_product_changes_nothing(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    outcome = await handlers.handle_event(session, event("products/delete", PRODUCT_ID))
    assert outcome == "unchanged"


async def test_an_old_update_after_the_delete_does_not_bring_the_product_back(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    """Out of order: the delete is processed first, then an update that was sent before it."""
    await add_product(session, shopify, make_product())
    await handlers.handle_event(session, event("products/delete", PRODUCT_ID))
    del shopify.products[PRODUCT_ID]  # Shopify: the product is gone
    await handlers.handle_event(session, event("products/update", PRODUCT_ID))
    assert not await visible(session)


# ---- inventory_levels/update


async def test_inventory_event_finds_the_product_and_updates_availability(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    await add_product(session, shopify, make_product())
    shopify.items[ITEM_ID] = PRODUCT_ID
    sold_out = make_variant(
        id=VARIANT_ID,
        product_id=PRODUCT_ID,
        available_for_sale=False,
        stock=StockState.OUT_OF_STOCK,
        inventory_quantity=0,
    )
    shopify.products[PRODUCT_ID] = make_product(variants=[sold_out])
    outcome = await handlers.handle_event(session, event("inventory_levels/update", ITEM_ID))
    assert outcome == "updated"
    available = (
        await session.execute(
            select(VariantMirror.available_for_sale).where(VariantMirror.id == VARIANT_ID)
        )
    ).scalar_one()
    assert available is False


async def test_inventory_event_of_an_item_without_a_product_is_ignored(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    outcome = await handlers.handle_event(session, event("inventory_levels/update", ITEM_ID))
    assert outcome == "ignored"


# ---- events that cannot be handled


async def test_event_without_a_resource_id_is_ignored(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    assert await handlers.handle_event(session, event("products/update", None)) == "ignored"


async def test_unknown_topic_is_ignored(session: AsyncSession, shopify: FakeShopify) -> None:
    assert await handlers.handle_event(session, event("orders/create", "x")) == "ignored"


async def test_an_id_that_is_not_a_shopify_id_is_ignored(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    shopify.bad_ids = True
    assert await handlers.handle_event(session, event("products/update", "nonsense")) == "ignored"


# ---- cache invalidation


async def test_the_live_cache_of_the_products_variants_is_cleared(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    shopify.products[PRODUCT_ID] = make_product()
    stock_mod._cache[VARIANT_ID] = (0.0, make_variant(id=VARIANT_ID, product_id=PRODUCT_ID))
    try:
        await handlers.handle_event(session, event("products/update", PRODUCT_ID))
        assert VARIANT_ID not in stock_mod._cache
    finally:
        stock_mod.clear_live_cache()


# ---- one worker pass: claim, finish, retry


async def test_a_pending_event_is_processed_and_marked_done(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    shopify.products[PRODUCT_ID] = make_product()
    event_id = await queue(session)
    assert await handlers.process_event(session) is True
    saved = await row(session, event_id)
    assert saved.status == "done"
    assert saved.processed_at is not None
    assert await visible(session)
    assert await handlers.process_event(session) is False  # nothing else is waiting


async def test_an_event_that_needs_no_work_is_marked_ignored(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    event_id = await queue(session, "inventory_levels/update", ITEM_ID)  # item has no product
    assert await handlers.process_event(session) is True
    assert (await row(session, event_id)).status == "ignored"


async def test_a_failing_event_waits_and_is_not_taken_again_at_once(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    shopify.down = True
    event_id = await queue(session)
    assert await handlers.process_event(session) is True
    saved = await row(session, event_id)
    assert (saved.status, saved.attempts) == ("pending", 1)
    assert saved.error == "Shopify is down (test)"
    assert saved.next_attempt_at is not None
    assert saved.next_attempt_at > datetime.now(UTC)
    assert await handlers.process_event(session) is False  # it must wait for its time


async def test_a_retry_that_is_due_is_taken_again_and_succeeds(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    shopify.products[PRODUCT_ID] = make_product()
    event_id = await queue(session)
    await session.execute(
        update(SyncEvent)
        .where(SyncEvent.id == event_id)
        .values(attempts=1, next_attempt_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    assert await handlers.process_event(session) is True
    saved = await row(session, event_id)
    assert saved.status == "done"
    assert saved.error is None


async def test_an_event_becomes_failed_after_the_last_attempt(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    shopify.down = True
    event_id = await queue(session)
    await session.execute(
        update(SyncEvent).where(SyncEvent.id == event_id).values(attempts=handlers.MAX_ATTEMPTS - 1)
    )
    assert await handlers.process_event(session) is True
    saved = await row(session, event_id)
    assert (saved.status, saved.attempts) == ("failed", handlers.MAX_ATTEMPTS)
    assert saved.processed_at is not None
    assert saved.next_attempt_at is None
    assert await handlers.process_event(session) is False  # a dead letter is never taken again


async def test_a_failing_handler_leaves_nothing_half_done(
    session: AsyncSession, shopify: FakeShopify, monkeypatch: pytest.MonkeyPatch
) -> None:
    shopify.products[PRODUCT_ID] = make_product()

    async def write_then_crash(
        db: AsyncSession, product_id: str, found: Product | None
    ) -> handlers.SyncOutcome:
        assert found is not None
        await repo.upsert_product(
            db, product_row(found, "h"), variant_rows(found), image_rows(found)
        )
        raise RuntimeError("crash in the middle")

    monkeypatch.setattr(handlers, "sync_product", write_then_crash)
    event_id = await queue(session)
    assert await handlers.process_event(session) is True
    assert await repo.product_state(session, PRODUCT_ID) is None  # the half write was undone
    saved = await row(session, event_id)
    assert (saved.status, saved.attempts, saved.error) == ("pending", 1, "RuntimeError")


# ---- the wait between tries


def test_the_wait_grows_and_then_stays_at_the_last_value() -> None:
    waits = [handlers.retry_seconds(n) for n in (1, 2, 3, 4, 5, 50)]
    assert waits == [30, 120, 600, 1800, 1800, 1800]
