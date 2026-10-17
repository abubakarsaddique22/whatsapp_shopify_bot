"""Phase 10, step 8: the cases the blueprint names, end to end on the real Postgres.

Already covered elsewhere (kept there on purpose):
    T16  a second delivery of the same event is ignored    test_sync_events.py
    T18  a changed policy or page is re-indexed            test_rag_ingest.py
    deleted product disappears (webhook)                   test_sync_handlers.py

This file adds: T17 (a price or stock change is seen) through the worker, events in the wrong
order, a handler that crashes and is retried, and a missed webhook repaired by the delta and the
nightly full pass. Every test is rolled back; "Shopify" is the FakeShopify of test_sync_handlers.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.catalog.importer import sync_mirror
from app.core.settings import get_settings
from app.db import repositories as repo
from app.db.tables import SyncEvent, VariantMirror
from app.domain.catalog import Money, StockState
from app.sync import handlers, reconcile
from tests.factories import make_product, make_variant
from tests.integration.test_sync_handlers import (
    ITEM_ID,
    PRODUCT_ID,
    VARIANT_ID,
    FakeShopify,
    add_product,
    event,
    queue,
    row,
    visible,
)

SECOND_ID = "gid://shopify/Product/9990002"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            # real events that wait in the dev database must not be taken by these tests
            await db.execute(
                update(SyncEvent).where(SyncEvent.status == "pending").values(status="ignored")
            )
            yield db
            await db.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def shopify(monkeypatch: pytest.MonkeyPatch) -> FakeShopify:
    fake = FakeShopify()
    monkeypatch.setattr(handlers.gateway, "get_product", fake.get_product)
    monkeypatch.setattr(
        handlers.gateway, "product_id_of_inventory_item", fake.product_id_of_inventory_item
    )
    return fake


async def title_in_mirror(session: AsyncSession, product_id: str = PRODUCT_ID) -> str | None:
    basics = await repo.product_basics(session, product_id)
    return None if basics is None else basics.title


async def make_due(session: AsyncSession, event_id: int) -> None:
    """Pretend the wait before a retry is over."""
    await session.execute(
        update(SyncEvent)
        .where(SyncEvent.id == event_id)
        .values(next_attempt_at=datetime.now(UTC) - timedelta(minutes=1))
    )


# ---- T17: a price or stock change is seen (through the worker, not only the handler)


async def test_t17_a_price_change_in_shopify_reaches_the_mirror_through_the_worker(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    await add_product(session, shopify, make_product())
    cheaper = make_variant(
        id=VARIANT_ID,
        product_id=PRODUCT_ID,
        price=Money(amount=Decimal("9900"), currency="PKR"),
    )
    shopify.products[PRODUCT_ID] = make_product(variants=[cheaper])
    event_id = await queue(session, "products/update", PRODUCT_ID)

    assert await handlers.process_event(session) is True

    assert (await row(session, event_id)).status == "done"
    price = (
        await session.execute(
            select(VariantMirror.price_amount).where(VariantMirror.id == VARIANT_ID)
        )
    ).scalar_one()
    assert price == Decimal("9900")


async def test_t17_a_sold_out_item_is_seen_through_the_worker(
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
    event_id = await queue(session, "inventory_levels/update", ITEM_ID)

    assert await handlers.process_event(session) is True

    assert (await row(session, event_id)).status == "done"
    available = (
        await session.execute(
            select(VariantMirror.available_for_sale).where(VariantMirror.id == VARIANT_ID)
        )
    ).scalar_one()
    assert available is False


# ---- events in the wrong order


async def test_two_updates_in_the_wrong_order_still_end_on_the_current_state(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    """The handler never uses the body of the event, it asks Shopify again. So an OLD event that is
    processed AFTER a newer one cannot bring old data back."""
    await add_product(session, shopify, make_product())
    older = await queue(session)
    newer = await queue(session)
    shopify.products[PRODUCT_ID] = make_product(title="Newest Title")  # Shopify knows only this

    # the newer event is taken first: the older one waits for a retry
    await session.execute(
        update(SyncEvent)
        .where(SyncEvent.id == older)
        .values(next_attempt_at=datetime.now(UTC) + timedelta(hours=1))
    )
    assert await handlers.process_event(session) is True
    assert (await row(session, newer)).status == "done"
    assert await title_in_mirror(session) == "Newest Title"

    await make_due(session, older)  # now the old event arrives late
    assert await handlers.process_event(session) is True
    assert (await row(session, older)).status == "done"
    assert await title_in_mirror(session) == "Newest Title"  # not changed back


async def test_an_update_for_a_product_the_mirror_has_never_seen_adds_it(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    """The 'create' event was lost or came later than the 'update'."""
    shopify.products[PRODUCT_ID] = make_product()
    assert await handlers.handle_event(session, event("products/update", PRODUCT_ID)) == "added"
    assert await visible(session)


async def test_a_late_create_after_a_delete_does_not_bring_the_product_back(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    await add_product(session, shopify, make_product())
    await handlers.handle_event(session, event("products/delete", PRODUCT_ID))
    del shopify.products[PRODUCT_ID]  # Shopify: it is gone
    outcome = await handlers.handle_event(session, event("products/create", PRODUCT_ID))
    assert outcome == "unchanged"
    assert not await visible(session)


# ---- a handler that crashes and is retried (the whole cycle)


async def test_shopify_is_down_then_up_the_event_is_retried_and_done(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    shopify.products[PRODUCT_ID] = make_product()
    shopify.down = True
    event_id = await queue(session)

    assert await handlers.process_event(session) is True  # first try fails
    failed_once = await row(session, event_id)
    assert (failed_once.status, failed_once.attempts) == ("pending", 1)
    assert not await visible(session)

    shopify.down = False  # Shopify is back
    await make_due(session, event_id)
    assert await handlers.process_event(session) is True  # the retry works

    done = await row(session, event_id)
    assert (done.status, done.error) == ("done", None)
    assert await visible(session)


async def test_a_dead_letter_event_works_again_after_a_retry_by_hand(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    """Step 7 + step 4 together: failed for good, then --retry-failed, then the worker succeeds."""
    shopify.down = True
    event_id = await queue(session)
    await session.execute(
        update(SyncEvent).where(SyncEvent.id == event_id).values(attempts=handlers.MAX_ATTEMPTS - 1)
    )
    await handlers.process_event(session)
    assert (await row(session, event_id)).status == "failed"

    shopify.down = False
    shopify.products[PRODUCT_ID] = make_product()
    assert await repo.requeue_failed_events(session, event_id) == 1
    assert await handlers.process_event(session) is True

    assert (await row(session, event_id)).status == "done"
    assert await visible(session)


# ---- a missed webhook is repaired by reconciliation


async def test_delta_repairs_an_edit_whose_webhook_never_came(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    """Server off, product edited, no event stored: the delta job refetches it."""
    await add_product(session, shopify, make_product())
    counts = await reconcile.apply_delta(
        session, {PRODUCT_ID: make_product(title="Edited while the server was off")}
    )
    assert counts["updated"] == 1
    assert await title_in_mirror(session) == "Edited while the server was off"


async def test_the_nightly_full_pass_repairs_a_missed_edit(session: AsyncSession) -> None:
    await sync_mirror(session, [make_product()])
    report = await sync_mirror(session, [make_product(title="Edited, webhook missed")])
    assert report.updated == 1
    assert await title_in_mirror(session) == "Edited, webhook missed"


async def test_the_nightly_full_pass_hides_a_product_deleted_while_the_server_was_off(
    session: AsyncSession,
) -> None:
    """Shopify does not list a deleted product, so only the full pass can find it (delta cannot)."""
    await sync_mirror(session, [make_product(), make_product(SECOND_ID)])
    assert await visible(session, SECOND_ID)

    report = await sync_mirror(session, [make_product()])  # the second one is gone from Shopify

    assert report.hidden >= 1  # (other real products of the dev mirror are hidden here too)
    assert await visible(session, PRODUCT_ID)
    assert not await visible(session, SECOND_ID)


async def test_an_empty_answer_from_shopify_never_wipes_the_mirror(session: AsyncSession) -> None:
    await sync_mirror(session, [make_product()])
    report = await sync_mirror(session, [])
    assert report.skipped_hiding is True
    assert report.hidden == 0
    assert await visible(session)


async def test_a_late_webhook_after_the_repair_finds_nothing_left_to_do(
    session: AsyncSession, shopify: FakeShopify
) -> None:
    """The nightly pass fixed the title first; the old webhook arrives afterwards."""
    await sync_mirror(session, [make_product()])
    shopify.products[PRODUCT_ID] = make_product(title="Fixed by the full pass")
    await sync_mirror(session, [make_product(title="Fixed by the full pass")])

    outcome = await handlers.handle_event(session, event("products/update", PRODUCT_ID))

    assert outcome == "unchanged"
    assert await title_in_mirror(session) == "Fixed by the full pass"
