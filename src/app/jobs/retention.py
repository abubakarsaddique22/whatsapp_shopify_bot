"""Retention job (Phase 11, step 9): delete data that is older than the RETENTION_* settings.

    uv run python -m app.jobs.retention              delete now (run it once a day)
    uv run python -m app.jobs.retention --dry-run    show what WOULD be deleted, change nothing

What goes, in this order (Blueprint 9.5; days come from the settings):

1. Conversations whose customer wrote nothing for RETENTION_CONVERSATION_DAYS: erased completely,
   the phone number included (messages, inbox, outbox, handoffs by cascade, plus their LangGraph
   checkpoints). A conversation with an OPEN handoff is skipped: staff still work on it.
2. Transcript lines older than RETENTION_MESSAGE_DAYS (the conversation itself stays).
3. Finished outbox rows (sent, failed) and inbox rows (done, ignored, failed) older than
   RETENTION_QUEUE_DAYS. Pending and processing rows are never touched.
4. Resolved handoffs older than RETENTION_HANDOFF_DAYS. Open ones are never touched.
5. security_events older than RETENTION_SECURITY_DAYS.
6. sync_events (Phase 10): done and ignored ones older than RETENTION_SYNC_DAYS; dead-letter
   (failed) ones are kept at least DEAD_LETTER_DAYS so somebody can still look at them. Pending
   and processing ones are never touched.
7. LangGraph checkpoints of conversations that wrote nothing for RETENTION_CHECKPOINT_DAYS, and
   checkpoints whose conversation no longer exists.

Not touched: the product mirror, the knowledge base, `sync_runs` (small: one row per run, and the
newest good run holds the watermark) and `runtime_flags`.

The job runs inside one SAVEPOINT: a --dry-run does the deletes and then undoes them, so the
numbers are exactly what a real run would delete.
"""

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import setup_logging
from app.core.settings import Settings, get_settings
from app.db.session import get_engine, get_sessionmaker
from app.db.tables import (
    Conversation,
    Handoff,
    Message,
    SecurityEvent,
    SyncEvent,
    WhatsAppInbox,
    WhatsAppOutbox,
)
from app.security.erasure import (
    delete_checkpoints,
    delete_idle_checkpoints,
    rows_changed,
)

log = logging.getLogger(__name__)

DEAD_LETTER_DAYS = 90  # failed sync events are kept at least this long
FINISHED_INBOX = ("done", "ignored", "failed")
FINISHED_OUTBOX = ("sent", "failed")
FINISHED_SYNC = ("done", "ignored")


class RetentionResult(BaseModel):
    dry_run: bool
    counts: dict[str, int] = Field(default_factory=dict)


async def _delete_idle_conversations(session: AsyncSession, cutoff: datetime) -> tuple[int, int]:
    """(conversations erased, checkpoint rows removed). Open handoffs protect a conversation."""
    last_seen = func.coalesce(Conversation.last_inbound_at, Conversation.created_at)
    has_open_handoff = (
        select(Handoff.id)
        .where(Handoff.conversation_id == Conversation.id, Handoff.status == "open")
        .exists()
    )
    result = await session.execute(
        select(Conversation.id).where(last_seen < cutoff, ~has_open_handoff)
    )
    ids = list(result.scalars().all())
    if not ids:
        return 0, 0
    checkpoints = await delete_checkpoints(session, ids)
    # ON DELETE CASCADE removes messages, inbox, outbox and handoffs of these conversations
    deleted = await session.execute(delete(Conversation).where(Conversation.id.in_(ids)))
    return rows_changed(deleted), checkpoints


async def _active_conversation_ids(session: AsyncSession, cutoff: datetime) -> list[int]:
    last_seen = func.coalesce(Conversation.last_inbound_at, Conversation.created_at)
    result = await session.execute(select(Conversation.id).where(last_seen >= cutoff))
    return list(result.scalars().all())


async def _apply(
    session: AsyncSession, settings: Settings, moment: datetime
) -> dict[str, int]:
    def before(days: int) -> datetime:
        return moment - timedelta(days=days)

    counts: dict[str, int] = {}
    counts["conversations"], counts["checkpoints"] = await _delete_idle_conversations(
        session, before(settings.retention_conversation_days)
    )
    counts["messages"] = rows_changed(
        await session.execute(
            delete(Message).where(Message.created_at < before(settings.retention_message_days))
        )
    )
    queue_cutoff = before(settings.retention_queue_days)
    counts["outbox"] = rows_changed(
        await session.execute(
            delete(WhatsAppOutbox).where(
                WhatsAppOutbox.status.in_(FINISHED_OUTBOX), WhatsAppOutbox.created_at < queue_cutoff
            )
        )
    )
    counts["inbox"] = rows_changed(
        await session.execute(
            delete(WhatsAppInbox).where(
                WhatsAppInbox.status.in_(FINISHED_INBOX), WhatsAppInbox.received_at < queue_cutoff
            )
        )
    )
    counts["handoffs"] = rows_changed(
        await session.execute(
            delete(Handoff).where(
                Handoff.status == "resolved",
                func.coalesce(Handoff.resolved_at, Handoff.created_at)
                < before(settings.retention_handoff_days),
            )
        )
    )
    counts["security_events"] = rows_changed(
        await session.execute(
            delete(SecurityEvent).where(
                SecurityEvent.created_at < before(settings.retention_security_days)
            )
        )
    )
    counts["sync_events"] = rows_changed(
        await session.execute(
            delete(SyncEvent).where(
                SyncEvent.status.in_(FINISHED_SYNC),
                SyncEvent.received_at < before(settings.retention_sync_days),
            )
        )
    ) + rows_changed(
        await session.execute(
            delete(SyncEvent).where(
                SyncEvent.status == "failed",
                SyncEvent.received_at < before(max(settings.retention_sync_days, DEAD_LETTER_DAYS)),
            )
        )
    )
    active = await _active_conversation_ids(session, before(settings.retention_checkpoint_days))
    counts["checkpoints"] += await delete_idle_checkpoints(session, active)
    return counts


async def run_retention(
    session: AsyncSession,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> RetentionResult:
    """Delete what is past its retention time. The caller commits (or, for a dry run, nothing
    is left to commit: the savepoint is rolled back)."""
    chosen = settings or get_settings()
    moment = now or datetime.now(UTC)
    savepoint = await session.begin_nested()
    try:
        counts = await _apply(session, chosen, moment)
    except BaseException:
        await savepoint.rollback()
        raise
    if dry_run:
        await savepoint.rollback()
    else:
        await savepoint.commit()
    log.info("retention_done", extra={"dry_run": dry_run, **counts})
    return RetentionResult(dry_run=dry_run, counts=counts)


async def main(dry_run: bool) -> int:
    setup_logging(get_settings().log_level)
    try:
        async with get_sessionmaker()() as session:
            result = await run_retention(session, dry_run=dry_run)
            if not dry_run:
                await session.commit()
    finally:
        await get_engine().dispose()
    print("Retention (dry run, nothing deleted):" if dry_run else "Retention, deleted:")
    for key, value in result.counts.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Delete data older than the retention settings.")
    parser.add_argument("--dry-run", action="store_true", help="show the numbers, change nothing")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run)))
