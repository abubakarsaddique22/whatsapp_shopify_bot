"""Background worker: turn inbound messages into replies, then send the queued replies.

Run it in its own window:  uv run python -m app.worker

Each pass does two small jobs, each in its own transaction:
1. process ONE pending inbound message and queue its reply in the outbox
2. send ONE pending reply from the outbox (sender.py has the retry rules)
A crash in the middle rolls the transaction back, so the row stays 'pending' and is retried.
"""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.core.logging import setup_logging
from app.core.settings import get_settings
from app.db import repositories as repo
from app.db.session import get_engine, get_sessionmaker
from app.db.tables import WhatsAppInbox
from app.integrations.whatsapp.sender import send_one

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # tries per inbound message before it becomes 'failed'
POLL_SECONDS = 2  # wait when there is nothing to do

UNSUPPORTED_REPLY = "Sorry, I can only read text messages right now. Please type your question."


def handle_text(text: str) -> list[str]:
    """Placeholder. Later phases replace it with the real assistant (tools, LangGraph)."""
    return ["Received"]


def handle_message(msg_type: str, text: str | None) -> list[str]:
    """Decide the replies for one inbound message. Returns one or more reply texts."""
    if msg_type == "text" and text:
        return handle_text(text)
    # Phase 6 adds an 'audio' branch here. Images, stickers and the rest stay unsupported.
    return [UNSUPPORTED_REPLY]


async def handle_inbox(session: AsyncSession, inbox: WhatsAppInbox) -> None:
    """Do the work for one claimed inbound message. The caller commits."""
    if get_settings().kill_switch:
        await repo.finish_inbox(session, inbox.id, "ignored")
        log.warning("kill_switch_on", extra={"inbox_id": inbox.id})
        return

    await repo.add_message(
        session, inbox.conversation_id, "in", inbox.text or f"[{inbox.msg_type}]", inbox.wamid
    )
    for seq, body in enumerate(handle_message(inbox.msg_type, inbox.text)):
        await repo.add_outbox(session, inbox.conversation_id, inbox.id, seq, body)
    await repo.finish_inbox(session, inbox.id, "done")


async def process_inbox(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    """Process one pending inbound message. Returns False if there was none."""
    async with sessionmaker() as session:
        inbox = await repo.claim_inbox(session)
        if inbox is None:
            return False
        inbox_id = inbox.id
        try:
            await handle_inbox(session, inbox)
            await session.commit()
        except Exception as exc:
            await session.rollback()  # nothing was half-done; also releases the row lock
            error = exc.message if isinstance(exc, AppError) else type(exc).__name__
            log.exception("inbox_failed", extra={"inbox_id": inbox_id})
            await repo.record_inbox_failure(session, inbox_id, error, MAX_ATTEMPTS)
            await session.commit()
        return True


async def send_outbox(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    """Send one pending reply. Returns False if there was none (or we must wait)."""
    if get_settings().kill_switch:
        return False  # the kill switch stops replies
    async with sessionmaker() as session:
        try:
            did_work = await send_one(session)
            await session.commit()
        except Exception:
            # for example missing WhatsApp settings: the row stays pending, we wait and retry
            await session.rollback()
            log.exception("outbox_error")
            return False
        return did_work


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    sessionmaker = get_sessionmaker()
    log.info("worker_started")
    try:
        while True:
            try:
                processed = await process_inbox(sessionmaker)
                sent = await send_outbox(sessionmaker)
            except Exception:  # for example the database is down: wait and try again
                log.exception("worker_loop_error")
                await asyncio.sleep(5)
                continue
            if not (processed or sent):
                await asyncio.sleep(POLL_SECONDS)
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("worker_stopped")
