"""Shopify webhook: POST /webhook/shopify (Phase 10, step 2).

Same rule as the WhatsApp webhook: check the signature on the RAW body, store the event (a repeated
delivery is harmless) and answer 200 quickly. The sync worker does the real work later (step 4).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidSignature
from app.core.security import valid_shopify_hmac
from app.core.settings import get_settings
from app.db import repositories
from app.db.session import get_session
from app.sync.webhooks import SUPPORTED_TOPICS, parse_delivery

log = logging.getLogger(__name__)
security_log = logging.getLogger("security")

router = APIRouter(tags=["shopify"])


@router.post("/webhook/shopify")
async def receive(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, str]:
    raw = await request.body()  # RAW bytes: the signature is computed on exactly these bytes
    settings = get_settings()
    if not valid_shopify_hmac(
        raw,
        request.headers.get("X-Shopify-Hmac-Sha256"),
        settings.shopify_client_secret.get_secret_value(),
    ):
        security_log.warning("invalid_shopify_hmac")
        raise InvalidSignature("Invalid Shopify signature")  # handler answers 401

    delivery = parse_delivery(request.headers, raw)
    if delivery is None:
        log.warning("shopify_webhook_unusable")
        return {"status": "ignored"}  # 200: a retry of the same delivery could never work

    if delivery.topic not in SUPPORTED_TOPICS:
        log.info("shopify_webhook_topic_ignored", extra={"topic": delivery.topic})
        return {"status": "ignored"}

    expected_shop = settings.shopify_shop_domain.lower()
    if expected_shop and delivery.shop_domain.lower() != expected_shop:
        security_log.warning("shopify_webhook_wrong_shop")
        return {"status": "ignored"}

    stored_id = await repositories.add_sync_event(session, delivery)
    await session.commit()
    log.info(
        "shopify_webhook",
        extra={"topic": delivery.topic, "new": stored_id is not None},
    )
    return {"status": "ok" if stored_id is not None else "duplicate"}
