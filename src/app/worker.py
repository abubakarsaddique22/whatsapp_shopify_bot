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

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.graph import (
    build_graph,
    open_checkpointer,
    run_turn,
    to_replies,
    use_selector_event_loop,
)
from app.agent.image_flow import Reply, run_image_flow
from app.agent.language_step import format_debug_reply, run_language_step
from app.agent.nodes import Deps
from app.agent.state import Source
from app.core.errors import AppError, UnsupportedMedia
from app.core.ids import correlation_id_var, new_correlation_id
from app.core.logging import setup_logging
from app.core.settings import Settings, get_settings
from app.core.tracing import configure_tracing
from app.db import repositories as repo
from app.db.session import get_engine, get_sessionmaker
from app.db.tables import WhatsAppInbox
from app.domain.models import Intent
from app.integrations.embeddings.factory import warm_up
from app.integrations.llm.base import ChatModel
from app.integrations.llm.chat_factory import get_chat_model
from app.integrations.speech.factory import get_stt_provider
from app.integrations.whatsapp.sender import send_one
from app.language.policy import reply_language
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
        prepare=dev_prepared_audio if is_dev_note else prepared_audio,
    )


async def handle_voice(
    session: AsyncSession, inbox: WhatsAppInbox, media_id: str, settings: Settings
) -> tuple[str, list[Reply]]:
    """One voice note -> (what to keep in the transcript, the replies).

    A clear transcript goes on like a typed message. A doubtful one gets a polite request to
    repeat, in the conversation's language, and nothing is answered from it. Only the transcript
    is kept, never the audio (blueprint 6.9).
    """
    try:
        outcome = await voice_outcome(media_id, settings)
    except UnsupportedMedia:
        return "[audio]", [Reply(UNSUPPORTED_REPLY)]  # the file is not audio at all

    transcript = outcome.transcript
    if transcript is None:
        state = await repo.load_state(session, inbox.conversation_id)
        language = reply_language(state.language, urdu_script_replies=settings.urdu_script_replies)
        return "[voice: unclear]", [Reply(clarification_message(outcome.reason, language))]

    return transcript.text, await reply_to_text(
        session, inbox.conversation_id, transcript.text, settings, source="voice"
    )


async def handle_inbox(session: AsyncSession, inbox: WhatsAppInbox) -> None:
    """Do the work for one claimed inbound message. The caller commits."""
    settings = get_settings()
    if settings.kill_switch:
        await repo.finish_inbox(session, inbox.id, "ignored")
        log.warning("kill_switch_on", extra={"inbox_id": inbox.id})
        return

    if inbox.msg_type == "audio" and inbox.media_id:
        kept, replies = await handle_voice(session, inbox, inbox.media_id, settings)
    else:
        kept = inbox.text or f"[{inbox.msg_type}]"
        if inbox.msg_type == "text" and inbox.text:
            replies = await reply_to_text(session, inbox.conversation_id, inbox.text, settings)
        else:  # image, sticker, empty text, audio without a media id: the polite reply
            replies = [Reply(UNSUPPORTED_REPLY)]

    await repo.add_message(session, inbox.conversation_id, "in", kept, inbox.wamid)
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
    await repo.finish_inbox(session, inbox.id, "done")


async def process_inbox(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    """Process one pending inbound message. Returns False if there was none."""
    correlation_id_var.set(new_correlation_id())  # one id per pass: logs and the trace share it
    async with sessionmaker() as session:
        inbox = await repo.claim_inbox(session)
        if inbox is None:
            return False
        inbox_id = inbox.id
        try:
            await handle_inbox(session, inbox)
            await session.commit()
        except Exception as exc:
            await session.rollback()  # nothing was half-done; also releases the row lock
            error = exc.message if isinstance(exc, AppError) else type(exc).__name__
            log.exception("inbox_failed", extra={"inbox_id": inbox_id})
            await repo.record_inbox_failure(session, inbox_id, error, MAX_ATTEMPTS)
            await session.commit()
        return True


async def send_outbox(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    """Send one pending reply. Returns False if there was none (or we must wait)."""
    if get_settings().kill_switch:
        return False  # the kill switch stops replies
    async with sessionmaker() as session:
        try:
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
        await get_engine().dispose()


if __name__ == "__main__":
    use_selector_event_loop()  # Windows: psycopg (the checkpointer) needs it, before asyncio.run
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("worker_stopped")
