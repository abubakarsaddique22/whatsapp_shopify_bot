"""Bot ON / OFF button for the store team (client decision 2026-10-21, replaces P8 owner hours).

The team answers customers from the WhatsApp Business app on the SAME number (Meta
"Coexistence"). Messages sent from the app are free; only the bot's messages cost money. So the
team has ONE button for all chats (the /team page, or `python -m app.owner off|on|status`):

1. Bot OFF = the team is answering. A new customer message WAITS team_wait_minutes (TEAM_WAIT_
   MINUTES, 3): worker.claim_next asks only for messages older than that.
2. When the team answers a customer from the app, Meta sends a `smb_message_echoes` webhook and
   record_owner_reply notes the moment (ConversationState.team_replied_at).
3. After the wait: a message the team answered is closed silently. A message NOBODY answered
   means the team forgot to switch the bot back on: the bot answers it AND turns itself ON
   (the team gets an email). The team switches it OFF again when they are back.
4. Bot ON = the bot answers every message at once, also in chats the team talked in before.
   Only a message the team ALREADY answered is never answered a second time by the bot.

    hold_cutoff(settings, now, bot_off)          # None = no waiting, else "received before this"
    team_answered(conv, received_at)             # True if the team replied after this message
    await record_owner_reply(session, echo, settings)

No message is sent to the customer about any of this: a "team will reply" notice would cost money
and the team's own reply is what the customer sees.
"""

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import hash_phone
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.domain.models import ConversationState
from app.integrations.whatsapp.payloads import OwnerEcho

log = logging.getLogger(__name__)

BOT_OFF_FLAG = "bot_off"  # runtime flag: on = the team is answering, the bot waits
BOT_AUTO_ON_FLAG = "bot_auto_on"  # on = the bot switched itself on (nobody answered in time)

AUTO_ON_ALERT = (
    "Bot khud ON ho gaya",
    "Bot OFF tha, lekin ek customer ko {minutes} minute mein team ka reply nahi mila, is liye "
    "bot ne usay jawab de diya aur ab bot ON hai. Team wapas aaye to /team page se bot OFF "
    "kar de.",
)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _offset(text: str) -> timezone:
    sign = -1 if text.startswith("-") else 1
    hours, minutes = text.lstrip("+-").split(":")
    return timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))


def local_time(moment: datetime, utc_offset: str) -> str:
    """'21 Oct 14:05' in the store's time zone, for the team page and the CLI."""
    return _aware(moment).astimezone(_offset(utc_offset)).strftime("%d %b %H:%M")


def hold_cutoff(settings: Settings, now: datetime, bot_off: bool) -> datetime | None:
    """Messages received AFTER the returned moment wait for the team. None = nothing waits."""
    if not bot_off:
        return None
    return _aware(now) - timedelta(minutes=settings.team_wait_minutes)


def team_answered(conv: ConversationState, received_at: datetime | None) -> bool:
    """Did the team reply to this customer from the app after this message arrived?"""
    if conv.team_replied_at is None or received_at is None:
        return False
    return _aware(conv.team_replied_at) >= _aware(received_at)


async def bot_is_off(session: AsyncSession) -> bool:
    return await repo.get_flag(session, BOT_OFF_FLAG)


async def set_bot(session: AsyncSession, on: bool, *, automatic: bool = False) -> None:
    """Switch the bot ON or OFF for all chats. The caller commits."""
    await repo.set_flag(session, BOT_OFF_FLAG, not on)
    await repo.set_flag(session, BOT_AUTO_ON_FLAG, on and automatic)
    log.warning("bot_switched", extra={"bot_on": on, "automatic": automatic})


async def record_owner_reply(
    session: AsyncSession, echo: OwnerEcho, settings: Settings
) -> int | None:
    """Handle one smb_message_echoes item. Returns the conversation id, or None if skipped.

    An echo of a reply the BOT sent is skipped. The team's text is kept in the transcript
    (direction 'out', like the bot's replies) so staff and the handoff summary see it.
    """
    if not echo.to_wa_id or await repo.is_our_outbound(session, echo.wamid):
        return None
    pepper = settings.phone_hash_pepper.get_secret_value()
    conversation_id = await repo.conversation_for_owner_reply(
        session, echo.to_wa_id, hash_phone(echo.to_wa_id, pepper)
    )
    conv = await repo.load_state(session, conversation_id)
    now = await repo.db_now(session)
    if conv.team_replied_at is None or _aware(conv.team_replied_at) < _aware(now):
        conv.team_replied_at = now
    await repo.save_state(session, conversation_id, conv)
    shown = echo.text if echo.msg_type == "text" and echo.text else f"[team {echo.msg_type}]"
    await repo.add_message(session, conversation_id, "out", shown, echo.wamid)
    log.info("team_replied", extra={"conversation_id": conversation_id})
    return conversation_id


# ---- `python -m app.owner off | on | status`


async def _switch(on: bool | None) -> str:
    from app.db.session import get_engine, get_sessionmaker

    settings = get_settings()
    try:
        async with get_sessionmaker()() as session:
            if on is not None:
                await set_bot(session, on)
                await session.commit()
            off = await bot_is_off(session)
            automatic = await repo.get_flag(session, BOT_AUTO_ON_FLAG)
    finally:
        await get_engine().dispose()
    if off:
        return (
            "bot: OFF (the team is answering)\n"
            f"a message nobody answers in {settings.team_wait_minutes} min turns the bot ON"
        )
    return "bot: ON" + (" (switched itself on: nobody answered in time)" if automatic else "")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.owner")
    parser.add_argument(
        "command",
        choices=["off", "on", "status"],
        help="off = the team answers (the bot waits); on = the bot answers at once",
    )
    args = parser.parse_args(argv)
    on = {"off": False, "on": True, "status": None}[args.command]
    print(asyncio.run(_switch(on)))
