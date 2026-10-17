"""Sync handlers and one worker pass (Phase 10, step 4).

A webhook only SAYS that something changed. The handler never trusts the body of the webhook: it
asks Shopify for the object by id and brings the mirror in line with the answer. That makes every
handler safe to repeat, and an old event can never overwrite newer data (it just refetches the
current state again).

    products/create, products/update   refetch the product, update the mirror
    products/delete                    hide the product (hidden, never deleted)
    inventory_levels/update            find the product of the inventory item, then as above

Price and stock a customer hears are always live from Shopify (Blueprint 4.6), so the mirror only
needs to follow what it stores: titles, options, variants, images and 'available for sale'.
"""

import logging
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalog import stock as stock_mod
from app.catalog.importer import SyncOutcome, sync_product
from app.core.errors import AppError, ValidationFailed
from app.core.ids import correlation_id_var, new_correlation_id
from app.db import repositories as repo
from app.db.tables import SyncEvent
from app.integrations.shopify import gateway

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # tries per event before it becomes 'failed' (the dead letter)
RETRY_SECONDS = (30, 120, 600, 1800)  # the wait after the 1st, 2nd, 3rd, 4th failure

Outcome = SyncOutcome | Literal["ignored"]


def retry_seconds(attempts_after: int) -> int:
    """How long to wait before the next try, given how many tries have failed so far."""
    return RETRY_SECONDS[min(max(attempts_after, 1), len(RETRY_SECONDS)) - 1]


# ---- the handlers


async def _hide(session: AsyncSession, product_id: str) -> Outcome:
    variant_ids = [row.id for row in await repo.variants_of(session, product_id)]
    stock_mod.forget_live_variants(variant_ids)
    return "hidden" if await repo.hide_product(session, product_id) else "unchanged"


async def _refresh_product(session: AsyncSession, product_id: str) -> Outcome:
    """Refetch one product and update the mirror. A ShopifyError is not caught here: the worker
    pass records it and the event is tried again later."""
    product = await gateway.get_product(product_id)  # None = deleted, draft, archived, unpublished
    old_ids = [row.id for row in await repo.variants_of(session, product_id)]
    new_ids = [v.id for v in product.variants] if product else []
    stock_mod.forget_live_variants([*old_ids, *new_ids])
    return await sync_product(session, product_id, product)


async def handle_event(session: AsyncSession, event: SyncEvent) -> Outcome:
    """Apply ONE event to the mirror. The caller owns the transaction."""
    resource_id = event.resource_id
    if not resource_id:
        return "ignored"
    try:
        if event.topic == "products/delete":
            return await _hide(session, resource_id)
        if event.topic in ("products/create", "products/update"):
            return await _refresh_product(session, resource_id)
        if event.topic == "inventory_levels/update":
            product_id = await gateway.product_id_of_inventory_item(resource_id)
            if product_id is None:
                return "ignored"  # an inventory item without a product: nothing to update
            return await _refresh_product(session, product_id)
    except ValidationFailed:  # not a Shopify id: a retry could never fix it
        log.warning("sync_event_bad_id", extra={"topic": event.topic})
        return "ignored"
    return "ignored"


# ---- one worker pass


async def process_event(session: AsyncSession) -> bool:
    """Claim and handle ONE pending event inside the open transaction, then commit once.

    The handler runs in a SAVEPOINT: if it fails, only its own writes are undone, the claim stays,
    and the failure (with the next try time) is saved in the same commit. A crash of the whole
    process rolls everything back, so the event is still 'pending' and is tried again.
    Returns False when there was nothing to do.
    """
    event = await repo.claim_sync_event(session)
    if event is None:
        return False
    event_id, attempts_before, topic = event.id, event.attempts, event.topic
    try:
        async with session.begin_nested():
            outcome = await handle_event(session, event)
    except Exception as exc:
        error = exc.message if isinstance(exc, AppError) else type(exc).__name__
        log.exception("sync_event_failed", extra={"event_id": event_id, "topic": topic})
        dead = await repo.record_sync_failure(
            session,
            event_id,
            error,
            attempts_before=attempts_before,
            max_attempts=MAX_ATTEMPTS,
            retry_in_seconds=retry_seconds(attempts_before + 1),
        )
        if dead:
            log.error("sync_event_dead_letter", extra={"event_id": event_id, "topic": topic})
    else:
        await repo.finish_sync_event(
            session, event_id, "ignored" if outcome == "ignored" else "done"
        )
        log.info(
            "sync_event_done",
            extra={"event_id": event_id, "topic": topic, "outcome": outcome},
        )
    await session.commit()
    return True


async def process_one(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    """One pass of the sync worker. Returns False if there was no event to process."""
    correlation_id_var.set(new_correlation_id())  # logs of one event share one id
    async with sessionmaker() as session:
        return await process_event(session)
