"""Send pending replies from the outbox, one row at a time.

Retry rules (Blueprint 5.7):
- temporary error (network, 429, 5xx): the row goes back to 'pending' and is tried again later,
  up to MAX_ATTEMPTS, then it becomes 'failed' (dead letter).
- any other WhatsApp error (400, 401, 403...): 'failed' at once, never retried. This includes
  the 24-hour window error, because retrying it forever would not help.

Picture rows (kind = 'image', Phase 7). `body` holds the plain-text fallback (name + link):
1. send by link (Shopify JPEG rendition URL; Meta downloads it);
2. if Meta refuses the link: download, convert to JPEG, upload, send by media id;
3. a temporary error is retried once (IMAGE_MAX_ATTEMPTS = 2 tries in total);
4. if the picture still cannot be delivered, the row becomes a normal text row holding the
   fallback, so the customer always gets the link. The failure is logged.

The caller owns the transaction: commit after `send_one()` returns.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.image_formats import ImageConversionError, jpeg_rendition_url
from app.core.errors import WhatsAppError, WhatsAppTemporaryError
from app.db import repositories as repo
from app.db.tables import WhatsAppOutbox
from app.integrations.whatsapp.client import send_image, send_text
from app.integrations.whatsapp.media_upload import fetch_and_prepare, upload_image

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
IMAGE_MAX_ATTEMPTS = 2  # one try and one retry, then the text fallback


def _describe(exc: WhatsAppError) -> str:
    """Short error text for the last_error column. No phone number, no token."""
    return f"{exc.message}, meta code {exc.detail.get('meta_code')}"


async def _deliver_image(wa_id: str, media_url: str, caption: str | None) -> str:
    """Send one picture and return its wamid: by link first, then by uploaded media id."""
    try:
        return await send_image(wa_id, link=jpeg_rendition_url(media_url), caption=caption)
    except WhatsAppTemporaryError:
        raise  # not the link's fault: the caller retries later
    except WhatsAppError as exc:
        log.warning("image_link_refused", extra={"meta_code": exc.detail.get("meta_code")})

    data, mime = await fetch_and_prepare(media_url)
    media_id = await upload_image(data, mime)
    return await send_image(wa_id, media_id=media_id, caption=caption)


async def _fall_back_to_text(session: AsyncSession, row: WhatsAppOutbox, reason: str) -> None:
    await repo.downgrade_outbox_to_text(session, row.id, reason)
    log.error("outbox_image_fallback", extra={"outbox_id": row.id})


async def _send_image_row(
    session: AsyncSession, row: WhatsAppOutbox, wa_id: str, media_url: str
) -> None:
    try:
        wamid = await _deliver_image(wa_id, media_url, row.caption)
    except WhatsAppTemporaryError as exc:  # must come first: it is a WhatsAppError too
        if row.attempts + 1 < IMAGE_MAX_ATTEMPTS:
            await repo.record_outbox_failure(
                session, row.id, _describe(exc), retry=True, max_attempts=IMAGE_MAX_ATTEMPTS
            )
            log.warning("outbox_image_retry", extra={"outbox_id": row.id})
        else:
            await _fall_back_to_text(session, row, _describe(exc))
    except WhatsAppError as exc:
        await _fall_back_to_text(session, row, _describe(exc))
    except ImageConversionError:
        await _fall_back_to_text(session, row, "picture could not be converted")
    else:
        await repo.mark_outbox_sent(session, row.id, wamid)
        await repo.add_message(session, row.conversation_id, "out", f"[image] {row.body}", wamid)
        log.info("outbox_sent", extra={"outbox_id": row.id, "kind": "image"})


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

    if row.kind == "image" and row.media_url:
        await _send_image_row(session, row, wa_id, row.media_url)
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
