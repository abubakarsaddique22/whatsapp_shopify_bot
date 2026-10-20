"""P8: owner first, bot as backup.

The store owner answers customers from the WhatsApp Business app on the SAME number (Meta
"Coexistence"). Messages sent from the app are free; only the bot's messages cost money. So:

1. In owner hours (OWNER_HOURS, or BUSINESS_HOURS) and with OWNER_FIRST=true, a new customer
   message WAITS for OWNER_WAIT_MINUTES (worker.claim_next asks for older messages only).
2. When the owner answers from the app, Meta sends a `smb_message_echoes` webhook. The bot then
   stays silent for that customer for OWNER_REPLY_PAUSE_MINUTES (record_owner_reply). This part
   works even with OWNER_FIRST=false: the bot never talks over the owner.
3. After the wait, a message the owner did not answer goes to the bot as usual.
4. Outside owner hours, or after `python -m app.owner away`, the bot answers at once.

    hold_cutoff(settings, now, away=False)       # None = no waiting, else "received before this"
    owner_is_handling(conv, now)                 # True while the owner's reply pause runs
    await record_owner_reply(session, echo, settings)

No message is sent to the customer about any of this: a "team will reply" notice would cost money
and the owner's own reply is what the customer sees.
"""

import argparse
import asyncio
import logging
from datetime import UTC, datetime, time, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import hash_phone
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.domain.models import ConversationState
from app.integrations.whatsapp.payloads import OwnerEcho

log = logging.getLogger(__name__)

OWNER_AWAY_FLAG = "owner_away"  # runtime flag: on = the owner is away, the bot answers at once


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _offset(text: str) -> timezone:
    sign = -1 if text.startswith("-") else 1
    hours, minutes = text.lstrip("+-").split(":")
    return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))


def owner_hours(settings: Settings) -> str:
    """OWNER_HOURS, or BUSINESS_HOURS when it is empty."""
    return settings.owner_hours or settings.business_hours


def in_hours(hours: str, utc_offset: str, now: datetime) -> bool:
    """Is `now` inside HH:MM-HH:MM (local time at utc_offset)? Start included, end not.
    The same start and end means all day; 22:00-02:00 runs over midnight."""
    start_text, end_text = hours.split("-")
    start, end = time.fromisoformat(start_text), time.fromisoformat(end_text)
    local = _aware(now).astimezone(_offset(utc_offset)).time()
    if start == end:
        return True
    if start < end:
        return start <= local < end
    return local >= start or local < end


def owner_available(settings: Settings, now: datetime, *, away: bool = False) -> bool:
    """Should the owner get the first chance to answer right now?"""
    if not settings.owner_first or away:
        return False
    return in_hours(owner_hours(settings), settings.business_utc_offset, now)


def hold_cutoff(settings: Settings, now: datetime, *, away: bool = False) -> datetime | None:
    """Messages received AFTER the returned moment wait for the owner. None = nothing waits."""
    if settings.owner_wait_minutes <= 0 or not owner_available(settings, now, away=away):
        return None
    return _aware(now) - timedelta(minutes=settings.owner_wait_minutes)


def owner_is_handling(conv: ConversationState, now: datetime) -> bool:
    """True while the pause after the owner's last reply to this customer runs."""
    if conv.owner_active_until is None:
        return False
    return _aware(conv.owner_active_until) > _aware(now)


def extend_owner_pause(conv: ConversationState, now: datetime, settings: Settings) -> None:
    """The owner just wrote to this customer: the bot is silent until now + the pause."""
    until = _aware(now) + timedelta(minutes=settings.owner_reply_pause_minutes)
    if conv.owner_active_until is None or _aware(conv.owner_active_until) < until:
        conv.owner_active_until = until


async def record_owner_reply(
    session: AsyncSession, echo: OwnerEcho, settings: Settings
) -> int | None:
    """Handle one smb_message_echoes item. Returns the conversation id, or None if skipped.

    An echo of a reply the BOT sent is skipped. The owner's text is kept in the transcript
    (direction 'out', like the bot's replies) so staff and the handoff summary see it.
    """
    if not echo.to_wa_id or await repo.is_our_outbound(session, echo.wamid):
        return None
    pepper = settings.phone_hash_pepper.get_secret_value()
    conversation_id = await repo.conversation_for_owner_reply(
        session, echo.to_wa_id, hash_phone(echo.to_wa_id, pepper)
    )
    conv = await repo.load_state(session, conversation_id)
    extend_owner_pause(conv, await repo.db_now(session), settings)
    await repo.save_state(session, conversation_id, conv)
    shown = echo.text if echo.msg_type == "text" and echo.text else f"[owner {echo.msg_type}]"
    await repo.add_message(session, conversation_id, "out", shown, echo.wamid)
    log.info("owner_replied", extra={"conversation_id": conversation_id})
    return conversation_id


# ---- `python -m app.owner away | back | status`


async def _set_away(away: bool | None) -> str:
    from app.db.session import get_engine, get_sessionmaker

    settings = get_settings()
    try:
        async with get_sessionmaker()() as session:
            if away is not None:
                await repo.set_flag(session, OWNER_AWAY_FLAG, away)
                await session.commit()
            is_away = await repo.get_flag(session, OWNER_AWAY_FLAG)
            now = await repo.db_now(session)
    finally:
        await get_engine().dispose()
    first = "on" if settings.owner_first else "off (OWNER_FIRST=false)"
    waiting = owner_available(settings, now, away=is_away)
    return (
        f"owner first: {first}\n"
        f"owner away: {'yes' if is_away else 'no'}\n"
        f"owner hours: {owner_hours(settings)} ({settings.business_utc_offset})\n"
        f"new messages wait {settings.owner_wait_minutes} min now: {'yes' if waiting else 'no'}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.owner")
    parser.add_argument(
        "command",
        choices=["away", "back", "status"],
        help="away = the bot answers at once; back = the owner gets the first chance again",
    )
    args = parser.parse_args(argv)
    away = {"away": True, "back": False, "status": None}[args.command]
    print(asyncio.run(_set_away(away)))
