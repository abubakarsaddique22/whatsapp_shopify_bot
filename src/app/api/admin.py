"""Phase 11, step 8: admin endpoints. OFF by default.

    GET  /admin/status                          kill switch state, number of open handoffs
    GET  /admin/handoffs                        the open handoffs (oldest first), for staff
    POST /admin/kill-switch   {"on": true}       stop the bot (the worker reads it per message)
    POST /admin/conversations/{id}/pause  {"hours": 12}   pause the bot for ONE customer
    POST /admin/conversations/{id}/resume       the bot answers this customer again

Rules (Blueprint 9.3):

* The router is included in main.py only when ADMIN_ENDPOINTS_ENABLED=true. Even then every call
  needs the header `X-Admin-Key` equal to ADMIN_API_KEY (24+ characters, checked at start-up by
  the settings). With the endpoints off the paths do not exist: 404.
* The kill switch is a row in `runtime_flags`, not a setting: the API and the worker are
  different processes. KILL_SWITCH=true in .env still works and cannot be undone from here.
* Every change leaves an `admin_action` security event (what and which conversation, never the
  customer's phone number or text). A wrong key leaves a log line, no row.
* Use it over HTTPS only (a tunnel or a load balancer): the key travels in a header.
"""

import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db import repositories as repo
from app.db.session import get_session
from app.handoff import service as handoff_service

log = logging.getLogger(__name__)
security_log = logging.getLogger("security")


def require_admin(x_admin_key: Annotated[str | None, Header()] = None) -> None:
    """404 while the endpoints are off, 401 for a missing or wrong key."""
    settings = get_settings()
    if not settings.admin_endpoints_enabled:
        raise HTTPException(status_code=404)
    key = settings.admin_api_key.get_secret_value()
    if not key or not x_admin_key or not hmac.compare_digest(x_admin_key.encode(), key.encode()):
        security_log.warning("admin_bad_key")
        raise HTTPException(status_code=401, detail="Invalid admin key")


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class KillSwitchBody(BaseModel):
    on: bool


class PauseBody(BaseModel):
    hours: int | None = Field(default=None, ge=1, le=720)  # None = HANDOFF_PAUSE_HOURS


async def _audit(session: AsyncSession, action: str, **detail: Any) -> None:
    await repo.add_security_event(session, "admin_action", detail={"action": action, **detail})
    log.warning("admin_action", extra={"admin_action": action, **detail})


async def _kill_state(session: AsyncSession) -> dict[str, bool]:
    flag = await repo.get_flag(session, repo.KILL_SWITCH_FLAG)
    setting = get_settings().kill_switch
    return {"flag": flag, "setting": setting, "active": flag or setting}


@router.get("/status")
async def status(session: SessionDep) -> dict[str, Any]:
    open_rows = await handoff_service.list_open(session, limit=1000)
    return {"kill_switch": await _kill_state(session), "open_handoffs": len(open_rows)}


@router.get("/handoffs")
async def handoffs(session: SessionDep) -> list[dict[str, Any]]:
    rows = await handoff_service.list_open(session)
    return [row._asdict() for row in rows]


@router.post("/kill-switch")
async def kill_switch(body: KillSwitchBody, session: SessionDep) -> dict[str, bool]:
    await repo.set_flag(session, repo.KILL_SWITCH_FLAG, body.on)
    await _audit(session, "kill_switch", on=body.on)
    await session.commit()
    return await _kill_state(session)


@router.post("/conversations/{conversation_id}/pause")
async def pause_conversation(
    conversation_id: int, session: SessionDep, body: PauseBody | None = None
) -> dict[str, Any]:
    hours = body.hours if body else None
    try:
        until = await handoff_service.pause(session, conversation_id, hours=hours)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such conversation") from None
    await _audit(session, "pause", conversation_id=conversation_id)
    await session.commit()
    return {"conversation_id": conversation_id, "paused_until": until.isoformat()}


@router.post("/conversations/{conversation_id}/resume")
async def resume_conversation(conversation_id: int, session: SessionDep) -> dict[str, Any]:
    try:
        result = await handoff_service.resume(session, conversation_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such conversation") from None
    await _audit(session, "resume", conversation_id=conversation_id)
    await session.commit()
    return {
        "conversation_id": conversation_id,
        "was_paused": result.was_paused,
        "handoffs_resolved": result.handoffs_resolved,
    }
