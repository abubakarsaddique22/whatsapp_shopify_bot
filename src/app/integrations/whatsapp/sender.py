"""Send pending replies from the outbox, one row at a time.

Retry rules (Blueprint 5.7):
- temporary error (network, 429, 5xx): the row goes back to 'pending' and is tried again later,
  up to MAX_ATTEMPTS, then it becomes 'failed' (dead letter).
- any other WhatsApp error (400, 401, 403...): 'failed' at once, never retried. This includes
  the 24-hour window error, because retrying it forever would not help.

The caller owns the transaction: commit after `send_one()` returns.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import WhatsAppError, WhatsAppTemporaryError
from app.db import repositories as repo
from app.integrations.whatsapp.client import send_text

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


def _describe(exc: WhatsAppError) -> str:
    """Short error text for the last_error column. No phone number, no token."""
    return f"{exc.message}, meta code {exc.detail.get('meta_code')}"


async def send_one(session: AsyncSession) -> bool:
    """Send the oldest pending reply. Returns False if there was nothing to send."""
    row = await repo.claim_outbox(session)
    if row is None:
        return False

    wa_id = await repo.conversation_wa_id(session, row.conversation_id)
    if wa_id is None:
        await repo.record_outbox_failure(
            session, row.id, "conversation not found", retry=False, max_attempts=MAX_ATTEMPTS
        )
        return True

    try:
        wamid = await send_text(wa_id, row.body)
    except WhatsAppTemporaryError as exc:  # must come first: it is a WhatsAppError too
        await repo.record_outbox_failure(
            session, row.id, _describe(exc), retry=True, max_attempts=MAX_ATTEMPTS
        )
        log.warning("outbox_send_failed", extra={"outbox_id": row.id, "retry": True})
    except WhatsAppError as exc:
        await repo.record_outbox_failure(
            session, row.id, _describe(exc), retry=False, max_attempts=MAX_ATTEMPTS
        )
        log.error(
            "outbox_send_failed",
            extra={"outbox_id": row.id, "retry": False, "meta_code": exc.detail.get("meta_code")},
        )
    else:
        await repo.mark_outbox_sent(session, row.id, wamid)
        await repo.add_message(session, row.conversation_id, "out", row.body, wamid)
        log.info("outbox_sent", extra={"outbox_id": row.id})
    return True
