"""Local-only helpers (APP_ENV=local). They do not exist in staging or production.

POST /dev/simulate  builds a signed customer message and sends it through the REAL webhook, so
                    the signature check, the duplicate check and the storage are all exercised.
                    Send the same `wamid` twice to test a duplicate delivery.
                    msg_type "audio" sends a fake voice note: the worker does not download it,
                    the fake speech provider "says" STT_FAKE_TEXT from .env (STT_PROVIDER=fake).
                    Change STT_FAKE_TEXT and restart the worker to test other sentences.
GET  /dev/outbox    shows the last replies the bot queued or sent (no phone numbers).
"""

import json
import uuid
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import sign_whatsapp
from app.core.settings import get_settings
from app.db.session import get_session
from app.db.tables import WhatsAppOutbox

router = APIRouter(prefix="/dev", tags=["dev"])


class SimulateRequest(BaseModel):
    text: str = "hello"
    wa_id: str = "920000000001"  # a made-up customer number
    wamid: str | None = None  # same wamid twice = Meta delivering the message again
    msg_type: Literal["text", "image", "audio"] = "text"


def _require_local() -> None:
    if get_settings().app_env != "local":
        raise HTTPException(status_code=404)


def _payload(body: SimulateRequest, wamid: str) -> dict[str, Any]:
    message: dict[str, Any] = {
        "from": body.wa_id,
        "id": wamid,
        "timestamp": "1760169600",
        "type": body.msg_type,
    }
    if body.msg_type == "text":
        message["text"] = {"body": body.text}
    elif body.msg_type == "audio":
        # "dev-" is how the worker knows this note is fake (local only, see worker.voice_outcome)
        message["audio"] = {"id": "dev-media", "mime_type": "audio/ogg; codecs=opus"}
    else:
        message["image"] = {"id": "dev-media", "mime_type": "image/jpeg"}
    value = {"messaging_product": "whatsapp", "messages": [message]}
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": value}]}]}


@router.post("/simulate")
async def simulate(body: SimulateRequest, request: Request) -> dict[str, Any]:
    _require_local()
    secret = get_settings().whatsapp_app_secret.get_secret_value()
    if not secret:
        raise HTTPException(status_code=400, detail="Set WHATSAPP_APP_SECRET in .env first")

    wamid = body.wamid or f"wamid.dev-{uuid.uuid4().hex[:12]}"
    raw = json.dumps(_payload(body, wamid)).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sign_whatsapp(raw, secret),
    }
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://dev") as http:
        response = await http.post("/webhook/whatsapp", content=raw, headers=headers)
    return {"wamid": wamid, "webhook_status": response.status_code, "webhook": response.json()}


@router.get("/outbox")
async def outbox(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, Any]]:
    _require_local()
    result = await session.execute(
        select(
            WhatsAppOutbox.id,
            WhatsAppOutbox.body,
            WhatsAppOutbox.status,
            WhatsAppOutbox.attempts,
            WhatsAppOutbox.last_error,
        )
        .order_by(WhatsAppOutbox.id.desc())
        .limit(10)
    )
    return [row._asdict() for row in result.all()]
