"""WhatsApp webhook: GET handshake with Meta, POST for incoming messages and status updates.

The POST handler only checks the signature, stores the messages (duplicates are harmless) and
answers 200 quickly. The worker does the real work later (Phase 4, step 5).
"""

import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidSignature
from app.core.ids import hash_phone
from app.core.security import valid_whatsapp_signature
from app.core.settings import get_settings
from app.db import repositories
from app.db.session import get_session
from app.integrations.whatsapp.payloads import parse_webhook

log = logging.getLogger(__name__)
security_log = logging.getLogger("security")

router = APIRouter(tags=["whatsapp"])


@router.get("/webhook/whatsapp")
async def verify(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
) -> PlainTextResponse:
    """Meta calls this once when you register the callback URL in the dashboard."""
    expected = get_settings().whatsapp_verify_token.get_secret_value()
    ok = (
        mode == "subscribe"
        and token is not None
        and challenge is not None
        and hmac.compare_digest(token.encode(), expected.encode())
    )
    if not ok:
        security_log.warning("whatsapp_verify_failed")
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(challenge)


@router.post("/webhook/whatsapp")
async def receive(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, str]:
    raw = await request.body()  # RAW bytes: the signature is computed on exactly these bytes
    settings = get_settings()
    if not valid_whatsapp_signature(
        raw,
        request.headers.get("X-Hub-Signature-256"),
        settings.whatsapp_app_secret.get_secret_value(),
    ):
        security_log.warning("invalid_whatsapp_signature")
        raise InvalidSignature("Invalid WhatsApp signature")  # handler answers 401

    try:
        payload = json.loads(raw)
    except ValueError:
        log.warning("whatsapp body is not JSON")
        return {"status": "ignored"}
    if not isinstance(payload, dict):
        return {"status": "ignored"}

    parsed = parse_webhook(payload)
    pepper = settings.phone_hash_pepper.get_secret_value()

    new_messages = 0
    for msg in parsed.messages:
        wa_id_hash = hash_phone(msg.wa_id, pepper)
        conversation_id = await repositories.get_or_create_conversation(
            session, msg.wa_id, wa_id_hash
        )
        if await repositories.add_inbox(session, conversation_id, wa_id_hash, msg) is not None:
            new_messages += 1

    for status in parsed.statuses:
        if status.status == "failed":
            log.warning("whatsapp_delivery_failed", extra={"meta_code": status.error_code})
            await repositories.mark_outbox_failed_by_wamid(
                session, status.wamid, status.error_code
            )

    await session.commit()
    log.info(
        "whatsapp_webhook",
        extra={
            "messages": len(parsed.messages),
            "new": new_messages,
            "statuses": len(parsed.statuses),
        },
    )
    return {"status": "ok"}
