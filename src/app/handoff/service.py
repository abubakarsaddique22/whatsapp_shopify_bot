"""Phase 11, step 5: human handoff. The record, the pause, the resume and the after-hours rule.

    record = await create_handoff(session, conversation_id, conv, "human_request")
    record.handoff_id, record.created        # created=False: this customer already had an open one

    pause_until(now, settings)               # when the bot may answer again (HANDOFF_PAUSE_HOURS)
    is_paused(conv)                          # True while conv.bot_paused_until is in the future
    await pause(session, conversation_id)    # pause one customer without a handoff (admin, step 8)
    await resume(session, conversation_id)   # staff: bot answers again, open handoff resolved
    await resume_if_expired(session, conversation_id, conv)   # the timer: same, after the pause

    is_open_now(settings)                    # BUSINESS_HOURS in the BUSINESS_UTC_OFFSET time zone
    after_hours_note("roman_ur", settings)   # sentence for the customer when closed, else None

    rows = await list_open(session)          # the handoff list for staff (oldest first)

What this module does and does not do (Blueprint 8.6):

* It writes the `handoffs` row and the conversation's pause. It sends nothing: telling staff is
  handoff/notifier.py (step 6), and the reply to the customer is a template made by the graph.
* One open handoff per conversation. A second request while one is open returns the same id
  (`created=False`), so a customer who asks three times does not make three tickets.
* The summary is built from the conversation by plain code (reason, language, product, the last
  few customer lines, clipped). No model call: a handoff must still work when the model is the
  thing that failed (safe_fallback). Card-like numbers are masked in it.
* The pause is `ConversationState.bot_paused_until`, saved in conversations.state like the rest of
  the conversation. The graph's handoff_node already sets it; `pause` is for the other callers.
* Business hours are only used to tell the customer when the team is back. The bot keeps working
  after hours and the handoff is recorded either way. The offset is a fixed UTC offset
  (BUSINESS_UTC_OFFSET), so no time-zone database is needed on Windows.

Wiring into the worker (read the pause, create the record after a handoff turn, add the
after-hours note) is step 7. Nothing here is called by the graph or the worker yet.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from typing import Any, cast

from sqlalchemy import CursorResult, Row, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.templates import render
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.db.tables import Handoff
from app.domain.models import ConversationState
from app.language.policy import ReplyLang
from app.security.redaction import redact_cards

log = logging.getLogger(__name__)

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"

URGENT_REASONS = frozenset({"complaint"})  # everything else is "normal"
MAX_REASON_CHARS = 40  # the column size
SUMMARY_CUSTOMER_LINES = 3  # the last customer messages shown to staff, not the whole chat
SUMMARY_LINE_CHARS = 140


# ---- the pause


def pause_until(now: datetime, settings: Settings | None = None) -> datetime:
    """The moment the bot may answer this customer again."""
    chosen = settings or get_settings()
    return now + timedelta(hours=chosen.handoff_pause_hours)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def is_paused(conv: ConversationState, now: datetime | None = None) -> bool:
    """True while bot_paused_until is in the future. A pause that has ended is not a pause."""
    if conv.bot_paused_until is None:
        return False
    return _aware(conv.bot_paused_until) > _aware(now or datetime.now(UTC))


async def pause(
    session: AsyncSession,
    conversation_id: int,
    *,
    hours: int | None = None,
    now: datetime | None = None,
) -> datetime:
    """Pause the bot for ONE customer (no handoff record). Returns the end of the pause.

    `hours` defaults to HANDOFF_PAUSE_HOURS. The caller commits. LookupError: no such conversation.
    """
    if hours is not None and hours < 1:
        raise ValueError("a pause is at least 1 hour")
    moment = _aware(now or datetime.now(UTC))
    until = moment + timedelta(hours=hours) if hours else pause_until(moment)
    conv = await repo.load_state(session, conversation_id)
    conv.bot_paused_until = until
    await repo.save_state(session, conversation_id, conv)
    log.info("conversation_paused", extra={"conversation_id": conversation_id})
    return until


@dataclass(frozen=True)
class ResumeResult:
    was_paused: bool  # the conversation had a pause or a handoff flag
    handoffs_resolved: int  # open handoff rows closed by this call


async def resume(session: AsyncSession, conversation_id: int) -> ResumeResult:
    """The bot answers this customer again: clear the pause and the flag, resolve the handoff.

    The 'questions in a row' count starts again from 0, otherwise the very next unclear message
    would go straight back to a person. Safe to call twice. The caller commits.
    LookupError: no such conversation.
    """
    conv = await repo.load_state(session, conversation_id)
    was_paused = conv.bot_paused_until is not None or conv.needs_handoff
    conv.bot_paused_until = None
    conv.needs_handoff = False
    conv.handoff_reason = None
    conv.clarification_count = 0
    await repo.save_state(session, conversation_id, conv)
    result = await session.execute(
        update(Handoff)
        .where(Handoff.conversation_id == conversation_id, Handoff.status == STATUS_OPEN)
        .values(status=STATUS_RESOLVED, resolved_at=func.now())
        .execution_options(synchronize_session=False)
    )
    resolved = cast("CursorResult[Any]", result).rowcount or 0
    log.info(
        "handoff_resumed",
        extra={"conversation_id": conversation_id, "resolved": resolved, "was_paused": was_paused},
    )
    return ResumeResult(was_paused, resolved)


async def resume_if_expired(
    session: AsyncSession,
    conversation_id: int,
    conv: ConversationState,
    now: datetime | None = None,
) -> bool:
    """The timer: when the pause has ended, resume. True if it did.

    `conv` is changed in place too, so the caller keeps working with the resumed state.
    """
    if conv.bot_paused_until is None or is_paused(conv, now):
        return False
    await resume(session, conversation_id)
    conv.bot_paused_until = None
    conv.needs_handoff = False
    conv.handoff_reason = None
    conv.clarification_count = 0
    return True


# ---- business hours and the after-hours sentence


def _parse_hours(text: str) -> tuple[time, time]:
    start, end = text.split("-")
    return time.fromisoformat(start), time.fromisoformat(end)


def _offset(text: str) -> timezone:
    sign = -1 if text[0] == "-" else 1
    return timezone(sign * timedelta(hours=int(text[1:3]), minutes=int(text[4:6])))


def is_open_now(settings: Settings | None = None, now: datetime | None = None) -> bool:
    """Is the team working now? Start is included, end is not. 22:00-02:00 runs over midnight.

    The same start and end (10:00-10:00) means open all day.
    """
    chosen = settings or get_settings()
    start, end = _parse_hours(chosen.business_hours)
    local = _aware(now or datetime.now(UTC)).astimezone(_offset(chosen.business_utc_offset)).time()
    if start == end:
        return True
    if start < end:
        return start <= local < end
    return local >= start or local < end


def after_hours_note(
    lang: ReplyLang, settings: Settings | None = None, now: datetime | None = None
) -> str | None:
    """The sentence that tells the customer when the team is back, or None while it is open."""
    chosen = settings or get_settings()
    if is_open_now(chosen, now):
        return None
    return render("after_hours_note", lang, hours=chosen.business_hours)


# ---- the record


def urgency_for(reason: str) -> str:
    return "high" if reason in URGENT_REASONS else "normal"


def _clip(text: str, limit: int = SUMMARY_LINE_CHARS) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 3] + "..."


def build_summary(
    conv: ConversationState,
    reason: str,
    *,
    product_title: str | None = None,
    note: str | None = None,
) -> str:
    """Up to 5 short lines for staff (Blueprint 8.6): reason, language, product, what was said.

    Only the last few customer lines, each clipped, never the whole chat. Card-like numbers are
    masked again here, whatever the history held.
    """
    customer = [
        line["text"] for line in conv.history if line.get("role") == "customer" and line.get("text")
    ]
    lines = [
        f"Reason: {reason}",
        f"Language: {conv.language.value}",
        f"Product: {product_title or 'none discussed'}",
    ]
    if customer:
        said = " | ".join(_clip(text) for text in customer[-SUMMARY_CUSTOMER_LINES:])
        lines.append(f"Customer said: {said}")
    if note:
        lines.append(f"Note: {_clip(note)}")
    return redact_cards("\n".join(lines))


@dataclass(frozen=True)
class HandoffRecord:
    handoff_id: int
    created: bool  # False: an open handoff of this conversation already existed


async def create_handoff(
    session: AsyncSession,
    conversation_id: int,
    conv: ConversationState,
    reason: str,
    *,
    urgency: str | None = None,
    note: str | None = None,
) -> HandoffRecord:
    """Write the handoff row for a customer who needs a person. The caller commits.

    The pause itself is conv.bot_paused_until (the graph's handoff_node sets it and the finish
    node saves it). Idempotent: with an open handoff already there, nothing new is written.
    `note` is an optional short remark for staff; never put secrets or phone numbers in it.
    """
    title = None
    if conv.current_product_id:
        basics = await repo.product_basics(session, conv.current_product_id)
        title = basics.title if basics else None

    stmt = (
        insert(Handoff)
        .values(
            conversation_id=conversation_id,
            reason=reason[:MAX_REASON_CHARS],
            urgency=urgency or urgency_for(reason),
            summary=build_summary(conv, reason, product_title=title, note=note),
            language=conv.language.value,
            product_id=conv.current_product_id,
            status=STATUS_OPEN,
        )
        .on_conflict_do_nothing(
            index_elements=["conversation_id"], index_where=Handoff.status == STATUS_OPEN
        )
        .returning(Handoff.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is not None:
        # the reason is a short code, never customer text; no summary and no phone number here
        log.info("handoff_created", extra={"handoff_id": new_id, "handoff_reason": reason})
        return HandoffRecord(int(new_id), True)

    existing = await session.execute(
        select(Handoff.id).where(
            Handoff.conversation_id == conversation_id, Handoff.status == STATUS_OPEN
        )
    )
    return HandoffRecord(int(existing.scalar_one()), False)


async def list_open(session: AsyncSession, limit: int = 50) -> Sequence[Row[Any]]:
    """The handoff list for staff: open handoffs, oldest first."""
    result = await session.execute(
        select(
            Handoff.id,
            Handoff.conversation_id,
            Handoff.reason,
            Handoff.urgency,
            Handoff.summary,
            Handoff.language,
            Handoff.product_id,
            Handoff.created_at,
            Handoff.notified_at,
        )
        .where(Handoff.status == STATUS_OPEN)
        .order_by(Handoff.id)
        .limit(limit)
    )
    return cast("Sequence[Row[Any]]", result.all())
