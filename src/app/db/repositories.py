"""Database reads and writes: catalogue mirror (Phase 3) and WhatsApp inbox/outbox (Phase 4).

Plain functions that take an AsyncSession. The caller owns the transaction (commit/rollback).
Only product DATA is stored here; live price and stock always come from Shopify (Blueprint 4.6).
"""

from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Row, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import (
    Conversation,
    Message,
    ProductImage,
    ProductMirror,
    RuntimeFlag,
    SecurityEvent,
    SyncEvent,
    SyncRun,
    VariantMirror,
    WhatsAppInbox,
    WhatsAppOutbox,
)
from app.domain.models import ConversationState
from app.integrations.whatsapp.payloads import InboundMessage
from app.security.redaction import redact_cards
from app.sync.webhooks import ShopifyDelivery


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


async def conversation_for_owner_reply(session: AsyncSession, wa_id: str, wa_id_hash: str) -> int:
    """Conversation id for a customer the OWNER wrote to (P8). Unlike get_or_create_conversation
    it does not touch last_inbound_at: an owner message does not open a 24-hour window."""
    await session.execute(
        insert(Conversation)
        .values(wa_id=wa_id, wa_id_hash=wa_id_hash, state={})
        .on_conflict_do_nothing(index_elements=["wa_id"])
    )
    result = await session.execute(select(Conversation.id).where(Conversation.wa_id == wa_id))
    return result.scalar_one()


async def is_our_outbound(session: AsyncSession, wamid: str) -> bool:
    """True if this wamid belongs to a reply the bot itself sent (not an owner message)."""
    result = await session.execute(
        select(WhatsAppOutbox.id).where(WhatsAppOutbox.wamid == wamid).limit(1)
    )
    return result.scalar_one_or_none() is not None


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
            text=redact_cards(msg.text) if msg.text else msg.text,  # no card number is stored
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


async def claim_inbox(
    session: AsyncSession, received_before: datetime | None = None
) -> WhatsAppInbox | None:
    """Oldest pending inbound message. SKIP LOCKED: two workers never take the same row.

    `received_before` (P8 owner first): only messages that arrived before this moment; newer ones
    wait, so the owner can answer them first from the WhatsApp Business app.

    populate_existing: record_inbox_failure updates `attempts` with synchronize_session=False,
    so a session that already holds this row would hand back the stale count. The voice path
    needs the real count to know the LAST try (worker.handle_voice). Same fix as claim_outbox.
    """
    query = select(WhatsAppInbox).where(WhatsAppInbox.status == "pending")
    if received_before is not None:
        query = query.where(WhatsAppInbox.received_at <= received_before)
    result = await session.execute(
        query.order_by(WhatsAppInbox.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
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
    session: AsyncSession,
    conversation_id: int,
    inbox_id: int,
    seq: int,
    body: str,
    *,
    kind: str = "text",
    media_url: str | None = None,
    caption: str | None = None,
) -> None:
    """Queue one reply. (inbox_id, seq) is unique, so a retry never queues the same reply twice."""
    stmt = (
        insert(WhatsAppOutbox)
        .values(
            conversation_id=conversation_id,
            inbox_id=inbox_id,
            seq=seq,
            kind=kind,
            media_url=media_url,
            caption=caption,
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
        .execution_options(populate_existing=True)
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


async def downgrade_outbox_to_text(session: AsyncSession, outbox_id: int, reason: str) -> None:
    """The picture could not be delivered: turn the row into its plain-text fallback.

    The row goes back to 'pending' with a fresh attempt count, so the normal text retry rules
    apply to the fallback. `reason` is kept in last_error (no phone number, no token).
    """
    await session.execute(
        update(WhatsAppOutbox)
        .where(WhatsAppOutbox.id == outbox_id)
        .values(
            kind="text",
            media_url=None,
            caption=None,
            status="pending",
            attempts=0,
            last_error=f"image fallback: {reason}"[:500],
        )
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
        .values(
            conversation_id=conversation_id,
            direction=direction,
            body=redact_cards(body),  # no card number is stored
            wamid=wamid,
        )
        .on_conflict_do_nothing(index_elements=["wamid"])
    )
    await session.execute(stmt)


# ---- Phase 10: Shopify sync events


async def add_sync_event(session: AsyncSession, delivery: ShopifyDelivery) -> int | None:
    """Store one Shopify webhook delivery as 'pending'. Returns the sync_events id, or None if
    this webhook_id was already stored (a repeated delivery; the unique webhook_id makes that
    harmless, T16)."""
    stmt = (
        insert(SyncEvent)
        .values(
            webhook_id=delivery.webhook_id,
            topic=delivery.topic,
            shop_domain=delivery.shop_domain,
            resource_id=delivery.resource_id,
            triggered_at=delivery.triggered_at,
            payload=delivery.payload,
            status="pending",
            attempts=0,
        )
        .on_conflict_do_nothing(index_elements=["webhook_id"])
        .returning(SyncEvent.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def product_state(session: AsyncSession, product_id: str) -> tuple[str, bool] | None:
    """(content_hash, is_visible) of ONE product in the mirror, or None if it was never there."""
    result = await session.execute(
        select(ProductMirror.content_hash, ProductMirror.is_visible).where(
            ProductMirror.id == product_id
        )
    )
    row = result.one_or_none()
    return None if row is None else (row[0], row[1])


async def hide_product(session: AsyncSession, product_id: str) -> bool:
    """Hide ONE product (is_visible = false). True if it was visible before.

    Hidden, not deleted: search skips it, but the history stays. Same rule as hide_missing.
    """
    result = await session.execute(
        update(ProductMirror)
        .where(ProductMirror.id == product_id, ProductMirror.is_visible.is_(True))
        .values(is_visible=False)
    )
    return (cast("CursorResult[Any]", result).rowcount or 0) > 0


# The sync worker claims a row, does its work and commits ONCE (same idea as the inbox above).


async def claim_sync_event(session: AsyncSession) -> SyncEvent | None:
    """Oldest pending event that is due (a retry waits for next_attempt_at).

    SKIP LOCKED: two sync workers never take the same row."""
    result = await session.execute(
        select(SyncEvent)
        .where(
            SyncEvent.status == "pending",
            or_(SyncEvent.next_attempt_at.is_(None), SyncEvent.next_attempt_at <= func.now()),
        )
        .order_by(SyncEvent.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def finish_sync_event(session: AsyncSession, event_id: int, status: str) -> None:
    """status: done | ignored."""
    await session.execute(
        update(SyncEvent)
        .where(SyncEvent.id == event_id)
        .values(status=status, processed_at=func.now(), error=None, next_attempt_at=None)
        .execution_options(synchronize_session=False)
    )


async def record_sync_failure(
    session: AsyncSession,
    event_id: int,
    error: str,
    *,
    attempts_before: int,
    max_attempts: int,
    retry_in_seconds: int,
) -> bool:
    """Count one failed attempt. Back to 'pending' to retry after a wait, or 'failed' (dead
    letter) once max_attempts is reached. Returns True if the event is now 'failed'."""
    attempts = attempts_before + 1
    failed = attempts >= max_attempts
    await session.execute(
        update(SyncEvent)
        .where(SyncEvent.id == event_id)
        .values(
            attempts=attempts,
            error=error[:500],
            status="failed" if failed else "pending",
            next_attempt_at=None
            if failed
            else datetime.now(UTC) + timedelta(seconds=retry_in_seconds),
            processed_at=func.now() if failed else None,
        )
        .execution_options(synchronize_session=False)
    )
    return failed


# ---- Phase 10: reconciliation runs (sync_runs)


async def start_sync_run(session: AsyncSession, kind: str) -> tuple[int, datetime]:
    """Write a 'running' row. Returns (run id, started_at). Commit it at once, so a crash during
    the run still leaves a visible 'running' row behind."""
    result = await session.execute(
        insert(SyncRun)
        .values(kind=kind, status="running")
        .returning(SyncRun.id, SyncRun.started_at)
    )
    row = result.one()
    return int(row[0]), row[1]


async def last_ok_watermark(session: AsyncSession, kind: str) -> datetime | None:
    """Watermark of the latest run of this kind that finished 'ok'. A failed run never counts."""
    result = await session.execute(
        select(SyncRun.watermark)
        .where(SyncRun.kind == kind, SyncRun.status == "ok", SyncRun.watermark.is_not(None))
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def finish_sync_run(
    session: AsyncSession,
    run_id: int,
    status: str,
    counts: dict[str, Any],
    error: str | None,
    watermark: datetime | None,
) -> None:
    """status: ok | failed. The watermark is only given for an 'ok' run."""
    await session.execute(
        update(SyncRun)
        .where(SyncRun.id == run_id)
        .values(
            status=status,
            finished_at=func.now(),
            counts=counts,
            error=error[:500] if error else None,
            watermark=watermark,
        )
        .execution_options(synchronize_session=False)
    )


# ---- Phase 10: monitor (read only, plus the dead-letter retry)


async def sync_event_status_counts(session: AsyncSession) -> dict[str, int]:
    """{status: how many sync_events} (pending, done, ignored, failed ...)."""
    rows = await session.execute(select(SyncEvent.status, func.count()).group_by(SyncEvent.status))
    return {status: int(count) for status, count in rows.all()}


async def oldest_due_event_time(session: AsyncSession) -> datetime | None:
    """received_at of the oldest pending event that is due now. An event that waits for its next
    retry (next_attempt_at in the future) is not counted: it is waiting on purpose."""
    result = await session.execute(
        select(func.min(SyncEvent.received_at)).where(
            SyncEvent.status == "pending",
            or_(SyncEvent.next_attempt_at.is_(None), SyncEvent.next_attempt_at <= func.now()),
        )
    )
    return result.scalar_one_or_none()


async def list_failed_events(session: AsyncSession, limit: int = 20) -> Sequence[Row[Any]]:
    """The dead letter: events that failed for good, newest first."""
    result = await session.execute(
        select(
            SyncEvent.id,
            SyncEvent.topic,
            SyncEvent.resource_id,
            SyncEvent.attempts,
            SyncEvent.error,
            SyncEvent.received_at,
        )
        .where(SyncEvent.status == "failed")
        .order_by(SyncEvent.id.desc())
        .limit(limit)
    )
    return cast("Sequence[Row[Any]]", result.all())


async def requeue_failed_events(session: AsyncSession, event_id: int | None = None) -> int:
    """Give dead-letter events a fresh start: back to 'pending' with 0 attempts. One event, or
    all of them when event_id is None. Returns how many were requeued. The caller commits.

    Safe: a handler refetches the object from Shopify, so running an old event again can never
    bring back old data.
    """
    stmt = update(SyncEvent).where(SyncEvent.status == "failed")
    if event_id is not None:
        stmt = stmt.where(SyncEvent.id == event_id)
    result = await session.execute(
        stmt.values(
            status="pending", attempts=0, next_attempt_at=None, processed_at=None
        ).execution_options(synchronize_session=False)
    )
    return cast("CursorResult[Any]", result).rowcount or 0


async def last_sync_run(session: AsyncSession, kind: str) -> Row[Any] | None:
    """(status, started_at, finished_at, error) of the newest run of this kind, or None."""
    result = await session.execute(
        select(SyncRun.status, SyncRun.started_at, SyncRun.finished_at, SyncRun.error)
        .where(SyncRun.kind == kind)
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(1)
    )
    return cast("Row[Any] | None", result.one_or_none())


async def last_ok_run_finished(session: AsyncSession, kind: str) -> datetime | None:
    """When the newest run of this kind that ended 'ok' finished."""
    result = await session.execute(
        select(func.max(SyncRun.finished_at)).where(SyncRun.kind == kind, SyncRun.status == "ok")
    )
    return result.scalar_one_or_none()


# ---- Phase 11: security events (Blueprint 9.3 point 7, 9.4)


async def add_security_event(
    session: AsyncSession,
    kind: str,
    *,
    wa_hash: str | None = None,
    ref: str | None = None,
    detail: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> None:
    """Record one suspected attack or abuse. Never pass message text, secrets or a phone number:
    wa_hash is the keyed hash of the sender, detail holds short facts. The caller commits.
    created_at is for the rate limiter, which keeps all its times on one clock."""
    event = SecurityEvent(kind=kind, wa_hash=wa_hash, ref=ref, detail=detail or {})
    if created_at is not None:
        event.created_at = created_at
    session.add(event)
    await session.flush()


async def add_security_event_once(
    session: AsyncSession, kind: str, *, ref: str, detail: dict[str, Any]
) -> bool:
    """Like add_security_event, but not again when a row with the same kind, ref and detail
    already exists. A flagged document is found again on every sync run: one row is enough.
    Returns True when a row was written."""
    found = await session.execute(
        select(SecurityEvent.id)
        .where(SecurityEvent.kind == kind, SecurityEvent.ref == ref, SecurityEvent.detail == detail)
        .limit(1)
    )
    if found.first() is not None:
        return False
    await add_security_event(session, kind, ref=ref, detail=detail)
    return True


async def count_recent_security_events(
    session: AsyncSession, kinds: Collection[str], wa_hash: str, since: datetime
) -> int:
    """How many events of these kinds this sender caused since `since` (strikes, notices)."""
    result = await session.execute(
        select(func.count())
        .select_from(SecurityEvent)
        .where(
            SecurityEvent.kind.in_(list(kinds)),
            SecurityEvent.wa_hash == wa_hash,
            SecurityEvent.created_at >= since,
        )
    )
    return int(result.scalar_one())


# ---- Phase 11: counters for rate limits and the daily budget (Blueprint 9.6)


async def db_now(session: AsyncSession) -> datetime:
    """The database clock. received_at and created_at come from it, so limits compare with it."""
    return (await session.execute(select(func.now()))).scalar_one()


async def inbox_counts(
    session: AsyncSession, conversation_id: int, now: datetime
) -> tuple[int, int, int]:
    """Messages this sender sent in the last minute, hour and day, whatever happened to them."""
    row = (
        await session.execute(
            select(
                func.count().filter(WhatsAppInbox.received_at >= now - timedelta(minutes=1)),
                func.count().filter(WhatsAppInbox.received_at >= now - timedelta(hours=1)),
                func.count(),
            ).where(
                WhatsAppInbox.conversation_id == conversation_id,
                WhatsAppInbox.received_at >= now - timedelta(days=1),
            )
        )
    ).one()
    return int(row[0]), int(row[1]), int(row[2])


async def paid_message_counts(session: AsyncSession, since: datetime) -> tuple[int, int]:
    """(text messages, voice notes) received since `since` that the bot did not ignore.

    'ignored' rows (rate-limited, paused, handoff-only) cost nothing, so they are not counted.
    """
    row = (
        await session.execute(
            select(
                func.count().filter(WhatsAppInbox.msg_type == "text"),
                func.count().filter(WhatsAppInbox.msg_type == "audio"),
            ).where(WhatsAppInbox.received_at >= since, WhatsAppInbox.status != "ignored")
        )
    ).one()
    return int(row[0]), int(row[1])


# ---- Phase 11: runtime flags (the kill switch the admin API can flip, step 8)

KILL_SWITCH_FLAG = "kill_switch"


async def get_flag(session: AsyncSession, name: str) -> bool:
    """Is this runtime flag on? A flag that was never set is off."""
    result = await session.execute(select(RuntimeFlag.enabled).where(RuntimeFlag.name == name))
    return bool(result.scalar_one_or_none())


async def set_flag(session: AsyncSession, name: str, enabled: bool) -> None:
    """Turn a runtime flag on or off. The caller commits."""
    stmt = insert(RuntimeFlag).values(name=name, enabled=enabled)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["name"], set_={"enabled": enabled, "updated_at": func.now()}
        )
    )
