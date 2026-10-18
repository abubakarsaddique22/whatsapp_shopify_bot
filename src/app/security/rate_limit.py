"""Phase 11: per-sender rate limits, strikes and the daily budget (Blueprint 9.6).

    decision = await check_sender(session, conversation.id, conversation.wa_id_hash, settings)
    decision.action        # "allow" | "notify" | "silent"
    decision.reason        # "minute" | "hour" | "day" | "strikes" (None when allowed)

    status = await budget_status(session, settings)
    status                 # "off" | "ok" | "alarm" | "exhausted"

Limits: the messages of one sender in the last minute, hour and day (RATE_LIMIT_PER_*), counted
from whatsapp_inbox. No Redis: Postgres is shared by every process and survives a restart.

Over the limit the sender gets ONE polite message ("notify"); after that the bot stays silent
until the window has passed ("silent"). The one-notice rule uses security_events, so there is no
extra column. When several windows are over, the longest one decides how long the silence lasts.

Strikes: injection attempts (written by the graph's finish node when the router says
abuse_or_injection) and rate-limit notices are strikes. STRIKE_LIMIT strikes within STRIKE_WINDOW
put the sender in a
cool-down: one notice, then silence until the strikes are old enough. The cool-down notice is
not a strike itself, so a cool-down can never extend itself.

Budget: DAILY_COST_BUDGET against an ESTIMATE of the last 24 hours: text messages and voice notes
the bot did not ignore, times COST_PER_TEXT_MESSAGE and COST_PER_VOICE_MESSAGE. The model calls do
not report tokens yet, so this is a cost-attack guard, not an invoice. At 80 % a "budget_alarm"
event is written, at 100 % "budget_reached" and the worker switches to handoff-only (Step 7).
Each event is written once per UTC day.

This module only decides and records. Sending the polite message, replying "handoff only" and
alerting the owner is the worker's and the notifier's job.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db import repositories as repo

Action = Literal["allow", "notify", "silent"]
BudgetStatus = Literal["off", "ok", "alarm", "exhausted"]

STRIKE_KINDS = ("injection_suspected", "rate_limited")
STRIKE_LIMIT = 3
STRIKE_WINDOW = timedelta(hours=1)
ALARM_AT = 0.8  # share of the daily budget where the alarm is raised


@dataclass(frozen=True)
class LimitDecision:
    action: Action
    reason: str | None = None


ALLOW = LimitDecision("allow")


async def check_sender(
    session: AsyncSession,
    conversation_id: int,
    wa_hash: str,
    settings: Settings,
    now: datetime | None = None,
) -> LimitDecision:
    """Decide what to do with the message that was just stored. The caller commits.

    `now` is only for tests. By default the database clock is used, the same clock that stamped
    received_at and the events, so a drifting host clock cannot make a limit wrong.
    """
    now = now or await repo.db_now(session)
    per_minute, per_hour, per_day = await repo.inbox_counts(session, conversation_id, now)

    # (reason, how long the silence lasts) for every limit that is over
    over: list[tuple[str, timedelta]] = []
    if per_minute > settings.rate_limit_per_minute:
        over.append(("minute", timedelta(minutes=1)))
    if per_hour > settings.rate_limit_per_hour:
        over.append(("hour", timedelta(hours=1)))
    if per_day > settings.rate_limit_per_day:
        over.append(("day", timedelta(days=1)))
    strikes = await repo.count_recent_security_events(
        session, STRIKE_KINDS, wa_hash, now - STRIKE_WINDOW
    )
    if strikes >= STRIKE_LIMIT:
        over.append(("strikes", STRIKE_WINDOW))

    if not over:
        return ALLOW
    reason, window = max(over, key=lambda item: item[1])

    # a cool-down notice is not a strike; a rate-limit notice is
    notice_kind = "cooldown_notice" if reason == "strikes" else "rate_limited"
    already_told = await repo.count_recent_security_events(
        session, (notice_kind,), wa_hash, now - window
    )
    if already_told:
        return LimitDecision("silent", reason)

    await repo.add_security_event(
        session, notice_kind, wa_hash=wa_hash, detail={"reason": reason}, created_at=now
    )
    return LimitDecision("notify", reason)


async def budget_check(
    session: AsyncSession, settings: Settings, now: datetime | None = None
) -> tuple[BudgetStatus, str | None]:
    """Like budget_status, and also says which event this very call raised for the first time
    today ("budget_alarm" or "budget_reached"), so the worker can alert the owner exactly once.
    """
    if settings.daily_cost_budget <= 0:
        return "off", None
    now = (now or await repo.db_now(session)).astimezone(UTC)
    texts, voices = await repo.paid_message_counts(session, now - timedelta(days=1))
    spent = texts * settings.cost_per_text_message + voices * settings.cost_per_voice_message
    share = spent / settings.daily_cost_budget
    day = now.date().isoformat()

    if share >= 1:
        raised = await repo.add_security_event_once(
            session, "budget_reached", ref="daily", detail={"day": day}
        )
        return "exhausted", "budget_reached" if raised else None
    if share >= ALARM_AT:
        raised = await repo.add_security_event_once(
            session, "budget_alarm", ref="daily", detail={"day": day}
        )
        return "alarm", "budget_alarm" if raised else None
    return "ok", None


async def budget_status(
    session: AsyncSession, settings: Settings, now: datetime | None = None
) -> BudgetStatus:
    """Where the estimated spend of the last 24 hours stands against DAILY_COST_BUDGET."""
    return (await budget_check(session, settings, now))[0]
