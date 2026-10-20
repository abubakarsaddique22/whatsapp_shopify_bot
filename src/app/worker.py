"""Background worker: turn inbound messages into replies, then send the queued replies.

Run it in its own window:  uv run python -m app.worker

Each pass does two small jobs, each in its own transaction:
1. process ONE pending inbound message and queue its reply in the outbox
2. send ONE pending reply from the outbox (sender.py has the retry rules)
A crash in the middle rolls the transaction back, so the row stays 'pending' and is retried.
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import UTC

from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.graph import (
    build_graph,
    open_checkpointer,
    run_turn,
    to_replies,
    turn_summary,
    use_selector_event_loop,
)
from app.agent.image_flow import Reply, run_image_flow
from app.agent.language_step import format_debug_reply, run_language_step
from app.agent.nodes import Deps
from app.agent.state import Source
from app.agent.templates import render
from app.core.errors import AppError, ProviderError, UnsupportedMedia
from app.core.ids import correlation_id_var, new_correlation_id
from app.core.logging import setup_logging
from app.core.settings import Settings, get_settings
from app.core.tracing import configure_tracing
from app.db import repositories as repo
from app.db.session import get_engine, get_sessionmaker
from app.db.tables import WhatsAppInbox
from app.domain.models import ConversationState, Intent
from app.handoff.notifier import notify_owner, notify_pending
from app.handoff.service import after_hours_note, create_handoff, is_paused, resume_if_expired
from app.integrations.embeddings.factory import warm_up
from app.integrations.llm.base import ChatModel
from app.integrations.llm.chat_factory import get_chat_model
from app.integrations.speech.factory import get_stt_provider
from app.integrations.whatsapp.sender import send_one
from app.language.policy import ReplyLang, reply_language
from app.security.rate_limit import budget_check, check_sender
from app.security.redaction import CARD_PLACEHOLDER, redact_cards
from app.voice.convert import prepared_audio
from app.voice.gate import clarification_message
from app.voice.pipeline import VoiceOutcome, dev_prepared_audio, transcribe_voice_note

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # tries per inbound message before it becomes 'failed'
POLL_SECONDS = 2  # wait when there is nothing to do

UNSUPPORTED_REPLY = "Sorry, I can only read text messages right now. Please type your question."

_checkpointer: BaseCheckpointSaver | None = None  # set by main(); None = no checkpoints (tests)


def set_checkpointer(saver: BaseCheckpointSaver | None) -> None:
    """The LangGraph Postgres saver the worker hands to every graph (Blueprint 3.7)."""
    global _checkpointer
    _checkpointer = saver


async def debug_language_reply(
    session: AsyncSession, conversation_id: int, text: str, llm: ChatModel | None = None
) -> str:
    """Phase 5, step 6 (development only): the reply says what the router understood.

    Runs the language step (router + sticky language + reply language) and saves the new
    conversation state. Switched on by DEBUG_REPLY=true. Phase 9 replaces this.
    """
    result = await run_language_step(
        session,
        conversation_id,
        text,
        llm or get_chat_model("router"),
        urdu_script_replies=get_settings().urdu_script_replies,
    )
    return format_debug_reply(result)


async def debug_reply_to_text(
    session: AsyncSession, conversation_id: int, text: str, settings: Settings
) -> list[Reply]:
    """Development only (DEBUG_REPLY=true): the reply says what the router understood.

    This is the Phase 5/7 behaviour, kept so you can look at the router by itself: image_request
    still goes to the old image flow, everything else gets the debug line.
    """
    result = await run_language_step(
        session,
        conversation_id,
        text,
        get_chat_model("router"),
        urdu_script_replies=settings.urdu_script_replies,
    )
    if result.router.intent == Intent.IMAGE_REQUEST:
        return await run_image_flow(session, conversation_id, result.router, text)
    return [Reply(format_debug_reply(result))]


async def graph_reply(
    session: AsyncSession,
    conversation_id: int,
    text: str,
    settings: Settings,
    *,
    source: Source = "text",
) -> list[Reply]:
    """Phase 9: ONE customer message through the LangGraph workflow (Blueprint Figure 3).

    The graph routes, runs the permission gate and the tools, composes, validates and saves the
    conversation state once (finish node). The caller owns the transaction. A missing
    LLM / COMPOSER_MODEL setting raises ConfigError on purpose: no silent fake model.
    """
    conversation = await repo.load_state(session, conversation_id)
    deps = Deps(
        session=session,
        llm=get_chat_model("router"),
        composer_llm=get_chat_model("composer"),
        urdu_script_replies=settings.urdu_script_replies,
    )
    correlation_id = correlation_id_var.get()
    if correlation_id == "-":  # not inside a worker pass (tests, scripts)
        correlation_id = new_correlation_id()
    graph = build_graph(deps, _checkpointer)
    state = await run_turn(
        graph,
        conversation_id,
        text,
        conversation,
        source=source,
        correlation_id=correlation_id,
    )
    log.info("turn_done", extra=turn_summary(state))
    return to_replies(state)


async def reply_to_text(
    session: AsyncSession,
    conversation_id: int,
    text: str,
    settings: Settings,
    *,
    source: Source = "text",
) -> list[Reply]:
    """Phase 9: every customer text (typed, or a voice transcript) goes through the graph.

    DEBUG_REPLY=true keeps the old debug reply for development. The router runs once per
    message in both cases.
    """
    if settings.debug_reply:
        return await debug_reply_to_text(session, conversation_id, text, settings)
    return await graph_reply(session, conversation_id, text, settings, source=source)


async def voice_outcome(media_id: str, settings: Settings) -> VoiceOutcome:
    """Download, convert and transcribe one voice note, then run the confidence gate.

    Local development: with APP_ENV=local, STT_PROVIDER=fake and a media id that starts with
    'dev-' (what /dev/simulate sends) nothing is downloaded and the fake STT answers with
    STT_FAKE_TEXT. A real voice note from a phone always takes the real path.
    """
    is_dev_note = (
        settings.app_env == "local"
        and settings.stt_provider == "fake"
        and media_id.startswith("dev-")
    )
    return await transcribe_voice_note(
        media_id,
        get_stt_provider(settings),
        max_bytes=settings.max_audio_bytes,
        max_seconds=settings.max_audio_seconds,
        min_words=settings.stt_min_words,
        min_confidence=settings.stt_min_confidence,
        language_hint=settings.stt_language_hint.strip() or None,
        allowed_languages=settings.stt_allowed_language_set,
        retry_language=settings.stt_retry_language.strip() or None,
        prepare=dev_prepared_audio if is_dev_note else prepared_audio,
    )


async def handle_voice(
    session: AsyncSession, inbox: WhatsAppInbox, media_id: str, settings: Settings
) -> tuple[str, list[Reply]]:
    """One voice note -> (what to keep in the transcript, the replies).

    A clear transcript goes on like a typed message. A doubtful one gets a polite request to
    repeat, in the conversation's language, and nothing is answered from it. Only the transcript
    is kept, never the audio (blueprint 6.9).

    Speech provider down (failure drill stt_down): the error is raised so the worker tries again
    (record_inbox_failure); on the LAST try the customer is told honestly, in the conversation
    language, that voice notes cannot be read right now and to type the question. Before this the
    message ended 'failed' and the customer heard nothing.
    """
    try:
        outcome = await voice_outcome(media_id, settings)
    except UnsupportedMedia:
        return "[audio]", [Reply(UNSUPPORTED_REPLY)]  # the file is not audio at all
    except ProviderError:
        if inbox.attempts + 1 < MAX_ATTEMPTS:
            raise  # not the last try: the worker tries this voice note again
        log.warning("voice_provider_down", extra={"inbox_id": inbox.id})
        state = await repo.load_state(session, inbox.conversation_id)
        language = reply_language(state.language, urdu_script_replies=settings.urdu_script_replies)
        return "[voice: not transcribed]", [Reply(render("voice_unavailable", language))]

    transcript = outcome.transcript
    if transcript is None:
        state = await repo.load_state(session, inbox.conversation_id)
        language = reply_language(state.language, urdu_script_replies=settings.urdu_script_replies)
        return "[voice: unclear]", [Reply(clarification_message(outcome.reason, language))]

    if redact_cards(transcript.text) != transcript.text:
        # a card number was SAID in the voice note: it never goes to the model (Phase 11)
        state = await repo.load_state(session, inbox.conversation_id)
        language = reply_language(state.language, urdu_script_replies=settings.urdu_script_replies)
        return redact_cards(transcript.text), [Reply(render("card_warning", language))]

    return transcript.text, await reply_to_text(
        session, inbox.conversation_id, transcript.text, settings, source="voice"
    )


# ---- Phase 11: what happens around the graph (Blueprint 8.6, 9.6)


class TurnOutcome(BaseModel):
    """What one message leaves for AFTER the commit.

    Mail and Slack are slow. They must never run inside the transaction that holds the customer's
    row, and never delay the customer's reply (see handoff/notifier.py).
    """

    alerts: list[tuple[str, str]] = Field(default_factory=list)  # (subject, body) for the owner
    new_handoff: bool = False  # a new handoff row was written: staff must be told


_background: set[asyncio.Task[None]] = set()  # keeps a reference, or Python may drop the task


def budget_alert(kind: str) -> tuple[str, str]:
    """(subject, body) for the owner. No customer data in it."""
    if kind == "budget_reached":
        return (
            "[Bot] Daily budget reached: handoff-only mode",
            "The estimated cost of the last 24 hours reached DAILY_COST_BUDGET. Until it drops "
            "again, the bot passes every customer to a person and makes no model calls.",
        )
    return (
        "[Bot] Daily budget at 80 %",
        "The estimated cost of the last 24 hours is at 80 % of DAILY_COST_BUDGET.",
    )


async def queue_replies(session: AsyncSession, inbox: WhatsAppInbox, replies: list[Reply]) -> None:
    for seq, reply in enumerate(replies):
        await repo.add_outbox(
            session,
            inbox.conversation_id,
            inbox.id,
            seq,
            reply.body,
            kind=reply.kind,
            media_url=reply.media_url,
            caption=reply.caption,
        )


async def finish_without_model(
    session: AsyncSession, inbox: WhatsAppInbox, replies: list[Reply], *, keep: str | None = None
) -> None:
    """End a message that never reached the graph.

    It is 'ignored': it cost nothing, so the budget does not count it (repo.paid_message_counts).
    `keep` is the text for the transcript, so staff can read it later. Card numbers are masked
    again by repo.add_message.
    """
    if keep is not None:
        await repo.add_message(session, inbox.conversation_id, "in", keep, inbox.wamid)
    await queue_replies(session, inbox, replies)
    await repo.finish_inbox(session, inbox.id, "ignored")


async def handoff_only(
    session: AsyncSession,
    inbox: WhatsAppInbox,
    conv: ConversationState,
    lang: ReplyLang,
    settings: Settings,
    outcome: TurnOutcome,
) -> None:
    """The daily budget is used up (Blueprint 9.6): no model call, a person takes over.

    One open handoff per customer (create_handoff is idempotent), so staff get one ticket and not
    one per message. The customer hears it ONCE per day, after that the bot stays silent.
    """
    record = await create_handoff(session, inbox.conversation_id, conv, "budget_reached")
    outcome.new_handoff = record.created
    day = (await repo.db_now(session)).astimezone(UTC).date().isoformat()
    first = await repo.add_security_event_once(
        session, "budget_notice", ref=str(inbox.conversation_id), detail={"day": day}
    )
    notice: list[Reply] = []
    if first:
        notice.append(Reply(render("handoff_created", lang)))
        note = after_hours_note(lang, settings)
        if note:
            notice.append(Reply(note))
    await finish_without_model(session, inbox, notice, keep=inbox.text or f"[{inbox.msg_type}]")


async def after_turn(
    session: AsyncSession,
    inbox: WhatsAppInbox,
    before: ConversationState,
    settings: Settings,
    replies: list[Reply],
    outcome: TurnOutcome,
) -> list[Reply]:
    """A turn that ended in a handoff: write the record, and say when the team is back if closed.

    The graph's handoff_node sets needs_handoff and the pause. A flag that was not set before this
    turn means a NEW handoff. The pause itself is read at the top of handle_inbox.
    """
    after = await repo.load_state(session, inbox.conversation_id)
    if not after.needs_handoff or before.needs_handoff:
        return replies
    record = await create_handoff(
        session, inbox.conversation_id, after, after.handoff_reason or "unknown"
    )
    outcome.new_handoff = record.created
    lang = reply_language(after.language, urdu_script_replies=settings.urdu_script_replies)
    note = after_hours_note(lang, settings)
    return [*replies, Reply(note)] if note else replies


async def announce(sessionmaker: async_sessionmaker[AsyncSession], outcome: TurnOutcome) -> None:
    """After the commit: tell the owner (alerts) and staff (new handoff). Never raises.

    A handoff that nobody could be told about stays open with notify_error set, and the next
    notify_pending (next handoff, or `python -m app.handoff.notifier`) tries it again.
    """
    for subject, body in outcome.alerts:
        await notify_owner(subject, body)
    if outcome.new_handoff:
        try:
            async with sessionmaker() as session:
                await notify_pending(session)
                await session.commit()
        except Exception:
            log.exception("handoff_notify_error")


def announce_later(sessionmaker: async_sessionmaker[AsyncSession], outcome: TurnOutcome) -> None:
    """Run announce() in the background: the customer's reply is not held back by a slow mail."""
    if not outcome.alerts and not outcome.new_handoff:
        return
    task = asyncio.create_task(announce(sessionmaker, outcome))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def handle_inbox(session: AsyncSession, inbox: WhatsAppInbox) -> TurnOutcome:
    """Do the work for one claimed inbound message. The caller commits.

    Order, cheap and safe first: kill switch, rate limit, pause, daily budget, card numbers, and
    only then the graph. A message stopped before the graph costs nothing and ends 'ignored'.
    """
    settings = get_settings()
    outcome = TurnOutcome()
    if settings.kill_switch or await repo.get_flag(session, repo.KILL_SWITCH_FLAG):
        await repo.finish_inbox(session, inbox.id, "ignored")
        log.warning("kill_switch_on", extra={"inbox_id": inbox.id})
        return outcome

    conv = await repo.load_state(session, inbox.conversation_id)
    lang = reply_language(conv.language, urdu_script_replies=settings.urdu_script_replies)
    shown = inbox.text or f"[{inbox.msg_type}]"

    # 1. rate limit: ONE polite message, then silence (security/rate_limit.py)
    decision = await check_sender(session, inbox.conversation_id, inbox.wa_id_hash, settings)
    if decision.action != "allow":
        limit_notice: list[Reply] = []
        if decision.action == "notify":
            kind = "cooldown" if decision.reason == "strikes" else "rate_limited"
            limit_notice.append(Reply(render(kind, lang)))
        log.warning(
            "sender_limited",
            extra={
                "inbox_id": inbox.id,
                "limit_reason": decision.reason,
                "limit_action": decision.action,
            },
        )
        await finish_without_model(session, inbox, limit_notice)
        return outcome

    # 2. pause: the timer may have ended it; while it runs, ONE acknowledgement, then silence
    if await resume_if_expired(session, inbox.conversation_id, conv):
        log.info("pause_expired", extra={"inbox_id": inbox.id})
    if is_paused(conv):
        pause_notice: list[Reply] = []
        if conv.pause_acked_for != conv.bot_paused_until:
            conv.pause_acked_for = conv.bot_paused_until
            await repo.save_state(session, inbox.conversation_id, conv)
            pause_notice.append(Reply(render("paused_ack", lang)))
        await finish_without_model(session, inbox, pause_notice, keep=shown)
        return outcome

    # 3. daily budget: an alarm at 80 % (owner is told once), handoff-only at 100 %
    status, raised = await budget_check(session, settings)
    if raised is not None:
        outcome.alerts.append(budget_alert(raised))
    if status == "exhausted":
        await handoff_only(session, inbox, conv, lang, settings, outcome)
        return outcome

    # 4. a card number was typed: it is already masked in inbox.text; warn, no model call
    if inbox.msg_type == "text" and inbox.text and CARD_PLACEHOLDER in inbox.text:
        card_notice = [Reply(render("card_warning", lang))]
        await finish_without_model(session, inbox, card_notice, keep=inbox.text)
        return outcome

    if inbox.msg_type == "audio" and inbox.media_id:
        kept, replies = await handle_voice(session, inbox, inbox.media_id, settings)
    else:
        kept = inbox.text or f"[{inbox.msg_type}]"
        if inbox.msg_type == "text" and inbox.text:
            replies = await reply_to_text(session, inbox.conversation_id, inbox.text, settings)
        else:  # image, sticker, empty text, audio without a media id: the polite reply
            replies = [Reply(UNSUPPORTED_REPLY)]

    replies = await after_turn(session, inbox, conv, settings, replies, outcome)
    await repo.add_message(session, inbox.conversation_id, "in", kept, inbox.wamid)
    await queue_replies(session, inbox, replies)
    await repo.finish_inbox(session, inbox.id, "done")
    return outcome


async def process_inbox(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    """Process one pending inbound message. Returns False if there was none."""
    correlation_id_var.set(new_correlation_id())  # one id per pass: logs and the trace share it
    outcome: TurnOutcome | None = None
    async with sessionmaker() as session:
        inbox = await repo.claim_inbox(session)
        if inbox is None:
            return False
        inbox_id = inbox.id
        try:
            outcome = await handle_inbox(session, inbox)
            await session.commit()
        except Exception as exc:
            outcome = None  # rolled back: there is nothing to announce
            await session.rollback()  # nothing was half-done; also releases the row lock
            error = exc.message if isinstance(exc, AppError) else type(exc).__name__
            log.exception("inbox_failed", extra={"inbox_id": inbox_id})
            await repo.record_inbox_failure(session, inbox_id, error, MAX_ATTEMPTS)
            await session.commit()
    if outcome is not None:
        announce_later(sessionmaker, outcome)  # after the commit, in the background
    return True


async def send_outbox(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    """Send one pending reply. Returns False if there was none (or we must wait)."""
    if get_settings().kill_switch:
        return False  # the kill switch stops replies
    async with sessionmaker() as session:
        try:
            if await repo.get_flag(session, repo.KILL_SWITCH_FLAG):
                return False  # the same switch, flipped through the admin API
            did_work = await send_one(session)
            await session.commit()
        except Exception:
            # for example missing WhatsApp settings: the row stays pending, we wait and retry
            await session.rollback()
            log.exception("outbox_error")
            return False
        return did_work


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    configure_tracing(settings)  # .env -> os.environ, or LangSmith stays off
    sessionmaker = get_sessionmaker()
    log.info("worker_started")
    try:
        async with AsyncExitStack() as stack:
            try:
                set_checkpointer(await stack.enter_async_context(open_checkpointer()))
            except Exception:
                # The conversation itself lives in conversations.state, so the bot still works;
                # only the per-step LangGraph checkpoints (traces, crash look) are missing.
                log.exception("checkpointer_unavailable")
            try:  # load the embedding model now: the first policy question must not pay for it
                await warm_up()
            except Exception:
                log.exception("embedder_warmup_failed")  # the first policy question loads it
            while True:
                try:
                    processed = await process_inbox(sessionmaker)
                    sent = await send_outbox(sessionmaker)
                except Exception:  # for example the database is down: wait and try again
                    log.exception("worker_loop_error")
                    await asyncio.sleep(5)
                    continue
                if not (processed or sent):
                    await asyncio.sleep(POLL_SECONDS)
    finally:
        set_checkpointer(None)
        await asyncio.gather(*_background, return_exceptions=True)  # let the last alerts finish
        await get_engine().dispose()


if __name__ == "__main__":
    use_selector_event_loop()  # Windows: psycopg (the checkpointer) needs it, before asyncio.run
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("worker_stopped")
