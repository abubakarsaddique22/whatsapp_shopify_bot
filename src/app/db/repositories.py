"""Database reads and writes: catalogue mirror (Phase 3) and WhatsApp inbox/outbox (Phase 4).

Plain functions that take an AsyncSession. The caller owns the transaction (commit/rollback).
Only product DATA is stored here; live price and stock always come from Shopify (Blueprint 4.6).
"""

from collections.abc import Collection, Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, Row, case, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import (
    Conversation,
    Message,
    ProductImage,
    ProductMirror,
    VariantMirror,
    WhatsAppInbox,
    WhatsAppOutbox,
)
from app.domain.models import ConversationState
from app.integrations.whatsapp.payloads import InboundMessage


async def mirror_state(session: AsyncSession) -> dict[str, tuple[str, bool]]:
    """{product_id: (content_hash, is_visible)} for every product already in the mirror."""
    rows = await session.execute(
        select(ProductMirror.id, ProductMirror.content_hash, ProductMirror.is_visible)
    )
    return {pid: (content_hash, visible) for pid, content_hash, visible in rows.all()}


async def upsert_product(
    session: AsyncSession,
    product_row: dict[str, Any],
    variant_rows: list[dict[str, Any]],
    image_rows: list[dict[str, Any]],
) -> None:
    """Insert or update one product and replace its variants and images.

    Variants and images are deleted and re-inserted: a product has few of them, and this way a
    removed variant or image can never stay behind in the mirror.
    """
    stmt = insert(ProductMirror).values(**product_row)
    changes = {k: v for k, v in product_row.items() if k != "id"}
    await session.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=changes))

    product_id = product_row["id"]
    await session.execute(delete(VariantMirror).where(VariantMirror.product_id == product_id))
    await session.execute(delete(ProductImage).where(ProductImage.product_id == product_id))
    if variant_rows:
        await session.execute(insert(VariantMirror), variant_rows)
    if image_rows:
        await session.execute(insert(ProductImage), image_rows)


async def hide_missing(session: AsyncSession, seen_ids: Collection[str]) -> int:
    """Mark products that Shopify no longer shows as not visible. Returns how many changed.

    They are hidden, not deleted: search must skip them (is_visible = false) but history stays.
    """
    result = await session.execute(
        update(ProductMirror)
        .where(ProductMirror.is_visible.is_(True), ProductMirror.id.not_in(list(seen_ids)))
        .values(is_visible=False)
    )
    return cast("CursorResult[Any]", result).rowcount or 0


async def product_basics(session: AsyncSession, product_id: str) -> Row[Any] | None:
    """Mirror facts of ONE visible product, or None (missing and hidden look the same)."""
    result = await session.execute(
        select(
            ProductMirror.id,
            ProductMirror.title,
            ProductMirror.url,
            ProductMirror.vendor,
            ProductMirror.product_type,
            ProductMirror.tags,
            ProductMirror.options,
            ProductMirror.description_text,
        ).where(ProductMirror.id == product_id, ProductMirror.is_visible.is_(True))
    )
    return result.one_or_none()


async def variants_of(session: AsyncSession, product_id: str) -> Sequence[Row[Any]]:
    """Variant id, title and options from the mirror. No price and no stock: those are live."""
    result = await session.execute(
        select(VariantMirror.id, VariantMirror.title, VariantMirror.options)
        .where(VariantMirror.product_id == product_id)
        .order_by(VariantMirror.title)
    )
    return cast("Sequence[Row[Any]]", result.all())


# ---- Phase 4: WhatsApp inbox / outbox


async def get_or_create_conversation(session: AsyncSession, wa_id: str, wa_id_hash: str) -> int:
    """Conversation id for this phone number. Creates it on the first message and always
    refreshes last_inbound_at (the 24-hour customer-service window starts from it)."""
    stmt = (
        insert(Conversation)
        .values(wa_id=wa_id, wa_id_hash=wa_id_hash, state={}, last_inbound_at=func.now())
        .on_conflict_do_update(index_elements=["wa_id"], set_={"last_inbound_at": func.now()})
        .returning(Conversation.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def add_inbox(
    session: AsyncSession, conversation_id: int, wa_id_hash: str, msg: InboundMessage
) -> int | None:
    """Store one inbound message. Returns the inbox id, or None if this wamid was already stored
    (Meta delivers the same message more than once; the unique wamid makes that harmless)."""
    stmt = (
        insert(WhatsAppInbox)
        .values(
            wamid=msg.wamid,
            conversation_id=conversation_id,
            wa_id_hash=wa_id_hash,
            msg_type=msg.msg_type,
            text=msg.text,
            media_id=msg.media_id,
            mime_type=msg.mime_type,
            wa_timestamp=msg.wa_timestamp,
            status="pending",
            attempts=0,
        )
        .on_conflict_do_nothing(index_elements=["wamid"])
        .returning(WhatsAppInbox.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_outbox_failed_by_wamid(
    session: AsyncSession, wamid: str, meta_code: int | None
) -> None:
    """Meta told us (status webhook) that a message we sent was not delivered."""
    await session.execute(
        update(WhatsAppOutbox)
        .where(WhatsAppOutbox.wamid == wamid)
        .values(status="failed", last_error=f"delivery failed, meta code {meta_code}")
    )


# ---- Phase 4: worker and sender
# The worker claims a row, does its work and commits ONCE. If it crashes in between, the
# transaction rolls back and the row is still 'pending', so nothing is ever stuck half-done.


async def claim_inbox(session: AsyncSession) -> WhatsAppInbox | None:
    """Oldest pending inbound message. SKIP LOCKED: two workers never take the same row."""
    result = await session.execute(
        select(WhatsAppInbox)
        .where(WhatsAppInbox.status == "pending")
        .order_by(WhatsAppInbox.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return result.scalar_one_or_none()


async def finish_inbox(session: AsyncSession, inbox_id: int, status: str) -> None:
    """status: done | ignored."""
    await session.execute(
        update(WhatsAppInbox)
        .where(WhatsAppInbox.id == inbox_id)
        .values(status=status, processed_at=func.now())
        .execution_options(synchronize_session=False)
    )


async def record_inbox_failure(
    session: AsyncSession, inbox_id: int, error: str, max_attempts: int
) -> None:
    """Count one failed attempt. Back to 'pending' for a retry, or 'failed' after max_attempts."""
    await session.execute(
        update(WhatsAppInbox)
        .where(WhatsAppInbox.id == inbox_id)
        .values(
            attempts=WhatsAppInbox.attempts + 1,
            last_error=error[:500],
            status=case((WhatsAppInbox.attempts + 1 >= max_attempts, "failed"), else_="pending"),
        )
        .execution_options(synchronize_session=False)
    )


async def add_outbox(
    session: AsyncSession, conversation_id: int, inbox_id: int, seq: int, body: str
) -> None:
    """Queue one reply. (inbox_id, seq) is unique, so a retry never queues the same reply twice."""
    stmt = (
        insert(WhatsAppOutbox)
        .values(
            conversation_id=conversation_id,
            inbox_id=inbox_id,
            seq=seq,
            body=body,
            status="pending",
            attempts=0,
        )
        .on_conflict_do_nothing(index_elements=["inbox_id", "seq"])
    )
    await session.execute(stmt)


async def claim_outbox(session: AsyncSession) -> WhatsAppOutbox | None:
    """Oldest pending reply to send."""
    result = await session.execute(
        select(WhatsAppOutbox)
        .where(WhatsAppOutbox.status == "pending")
        .order_by(WhatsAppOutbox.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return result.scalar_one_or_none()


async def mark_outbox_sent(session: AsyncSession, outbox_id: int, wamid: str) -> None:
    await session.execute(
        update(WhatsAppOutbox)
        .where(WhatsAppOutbox.id == outbox_id)
        .values(
            status="sent",
            wamid=wamid,
            attempts=WhatsAppOutbox.attempts + 1,
            sent_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )


async def record_outbox_failure(
    session: AsyncSession, outbox_id: int, error: str, *, retry: bool, max_attempts: int
) -> None:
    """retry=True (temporary error): back to 'pending' until max_attempts. retry=False: failed."""
    status: Any
    if retry:
        status = case((WhatsAppOutbox.attempts + 1 >= max_attempts, "failed"), else_="pending")
    else:
        status = "failed"
    await session.execute(
        update(WhatsAppOutbox)
        .where(WhatsAppOutbox.id == outbox_id)
        .values(attempts=WhatsAppOutbox.attempts + 1, last_error=error[:500], status=status)
        .execution_options(synchronize_session=False)
    )


async def conversation_wa_id(session: AsyncSession, conversation_id: int) -> str | None:
    """The raw phone number, needed only to send a reply. Never log it."""
    result = await session.execute(
        select(Conversation.wa_id).where(Conversation.id == conversation_id)
    )
    return result.scalar_one_or_none()


# ---- Phase 5: conversation state (sticky language, last router output)


async def load_state(session: AsyncSession, conversation_id: int) -> ConversationState:
    """The saved ConversationState of this conversation. A new conversation (empty JSON)
    gets a fresh state. conversation_id and sender_hash always come from the row, so the JSON
    can never disagree with the table."""
    result = await session.execute(
        select(Conversation.state, Conversation.wa_id_hash).where(
            Conversation.id == conversation_id
        )
    )
    row = result.one_or_none()
    if row is None:
        raise LookupError(f"conversation {conversation_id} does not exist")
    saved, wa_id_hash = row
    data = {**(saved or {}), "conversation_id": str(conversation_id), "sender_hash": wa_id_hash}
    return ConversationState.model_validate(data)


async def save_state(session: AsyncSession, conversation_id: int, state: ConversationState) -> None:
    """Save the state as JSON and keep conversations.lang in step with state.language."""
    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(state=state.model_dump(mode="json"), lang=state.language.value)
        .execution_options(synchronize_session=False)
    )


async def add_message(
    session: AsyncSession,
    conversation_id: int,
    direction: str,
    body: str,
    wamid: str | None = None,
) -> None:
    """Add one turn (direction 'in' or 'out') to the conversation transcript."""
    stmt = (
        insert(Message)
        .values(conversation_id=conversation_id, direction=direction, body=body, wamid=wamid)
        .on_conflict_do_nothing(index_elements=["wamid"])
    )
    await session.execute(stmt)
