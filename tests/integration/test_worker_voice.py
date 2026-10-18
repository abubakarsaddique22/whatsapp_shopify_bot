"""Voice notes in the worker (Phase 6, step 4b): T10, T11, T12, T25, T22 on the real Postgres.

Only the download and the speech provider are fake: `worker.prepared_audio` is replaced by a
stand-in that records "open" and "closed", and `worker.get_stt_provider` hands out a FakeSTT. The
pipeline, the confidence gate, the language state and the database are the real ones.
Each test runs inside a transaction that is rolled back, so your real rows stay as they are.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import worker
from app.agent.templates import render
from app.core.errors import MediaTooLarge, TranscriptionError, UnsupportedMedia
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.db.tables import Message, WhatsAppInbox, WhatsAppOutbox
from app.domain.models import Lang
from app.integrations.llm.fake_llm import FakeLLM
from app.integrations.speech.fake_stt import FakeSTT, fake_result
from app.integrations.whatsapp.payloads import InboundMessage
from app.voice.convert import PreparedAudio
from app.voice.gate import GateReason, clarification_message

WA_ID = "920000000007"
WAV = Path("note.wav")
URDU = "اس کی قیمت کیا ہے"
PUNJABI = "اے گھڑی دی قیمت کی اے"
ENGLISH = "How much is this watch?"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            await db.execute(delete(WhatsAppOutbox))
            await db.execute(delete(WhatsAppInbox))
            yield db
            await db.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


# ---- helpers


def use_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Settings for the worker. Every value that matters here is written out on purpose, so a
    .env on this PC (a real STT provider, DEBUG_REPLY=true) cannot change the result."""
    values: dict[str, Any] = {
        "app_env": "local",
        "kill_switch": False,
        "debug_reply": False,
        "stt_provider": "fake",
        "stt_fake_text": "",
        "stt_min_words": 2,
        "stt_min_confidence": 0.5,
        "stt_allowed_languages": "en,ur,pa",
        "stt_language_hint": "",
        "max_audio_seconds": 90,
        "max_audio_bytes": 5_000_000,
        "urdu_script_replies": False,
        **overrides,
    }
    monkeypatch.setattr(worker, "get_settings", lambda: Settings(**values))
    # the router looks at every text now: an empty FakeLLM answers 'unclear' (no network). A test
    # that needs real router answers patches worker.get_chat_model again after this call.
    monkeypatch.setattr(worker, "get_chat_model", lambda role="router": FakeLLM([]))


def use_voice(
    monkeypatch: pytest.MonkeyPatch,
    stt: FakeSTT,
    *,
    duration_s: float = 4.0,
    error: Exception | None = None,
) -> list[str]:
    """Fake the speech provider and the download. Returns the list of "open"/"closed" events."""
    events: list[str] = []

    @asynccontextmanager
    async def fake_prepared_audio(media_id: str, *, max_bytes: int, max_seconds: int):
        if error is not None:
            raise error
        events.append("open")
        try:
            yield PreparedAudio(
                wav_path=WAV, duration_s=duration_s, size_bytes=50_000, mime_type="audio/ogg"
            )
        finally:
            events.append("closed")

    monkeypatch.setattr(worker, "get_stt_provider", lambda settings=None: stt)
    monkeypatch.setattr(worker, "prepared_audio", fake_prepared_audio)
    return events


def router_answer(language: str, intent: str = "price") -> str:
    return json.dumps({"language": language, "intent": intent, "confidence": 0.9})


async def conversation_id(session: AsyncSession) -> int:
    return await repo.get_or_create_conversation(session, WA_ID, "testhash")


async def queue_voice(
    session: AsyncSession, media_id: str | None = "MEDIA1", mime: str = "audio/ogg; codecs=opus"
) -> int:
    msg = InboundMessage(
        wamid=f"wamid.test-{uuid.uuid4().hex[:12]}",
        wa_id=WA_ID,
        msg_type="audio",
        media_id=media_id,
        mime_type=mime,
    )
    inbox_id = await repo.add_inbox(session, await conversation_id(session), "testhash", msg)
    assert inbox_id is not None
    return inbox_id


async def process(session: AsyncSession) -> int:
    """What the worker does for one message (without its own commit). Returns the inbox id."""
    inbox = await repo.claim_inbox(session)
    assert inbox is not None
    await worker.handle_inbox(session, inbox)
    return inbox.id


async def reply_bodies(session: AsyncSession) -> list[str]:
    result = await session.execute(select(WhatsAppOutbox.body).order_by(WhatsAppOutbox.id))
    return list(result.scalars().all())


async def kept_messages(session: AsyncSession) -> list[tuple[str, str]]:
    result = await session.execute(
        select(Message.direction, Message.body).where(
            Message.conversation_id == await conversation_id(session)
        )
    )
    return [(row.direction, row.body) for row in result.all()]


async def inbox_status(session: AsyncSession, inbox_id: int) -> str:
    result = await session.execute(select(WhatsAppInbox.status).where(WhatsAppInbox.id == inbox_id))
    return result.scalar_one()


# ---- T12, T10, T11: a clear voice note goes on like a typed message


async def test_t12_english_voice_note_reaches_the_router(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch, debug_reply=True)
    llm = FakeLLM([router_answer("en")])
    monkeypatch.setattr(worker, "get_chat_model", lambda role="router": llm)
    events = use_voice(monkeypatch, FakeSTT([fake_result(ENGLISH, language="en")]))

    await queue_voice(session)
    await process(session)

    assert len(llm.calls) == 1
    assert ENGLISH in llm.calls[0][1]  # the transcript is what the router read
    (body,) = await reply_bodies(session)
    assert body.startswith("[debug] language=en | intent=price")
    assert await kept_messages(session) == [("in", ENGLISH)]  # the transcript, never the audio
    assert events == ["open", "closed"]


async def test_t10_urdu_voice_note_reaches_the_router(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch, debug_reply=True)
    llm = FakeLLM([router_answer("ur")])
    monkeypatch.setattr(worker, "get_chat_model", lambda role="router": llm)
    use_voice(monkeypatch, FakeSTT([fake_result(URDU, language="ur", confidence=0.9)]))

    await queue_voice(session)
    await process(session)

    assert len(llm.calls) == 1
    (body,) = await reply_bodies(session)
    assert body.startswith("[debug] language=ur | intent=price")
    assert await kept_messages(session) == [("in", URDU)]


async def test_t11_punjabi_voice_note_is_handled_when_it_is_clear(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until the bake-off decides otherwise, a clear Punjabi transcript is not refused."""
    use_settings(monkeypatch, debug_reply=True)
    llm = FakeLLM([router_answer("pa")])
    monkeypatch.setattr(worker, "get_chat_model", lambda role="router": llm)
    use_voice(monkeypatch, FakeSTT([fake_result(PUNJABI, language="pa", confidence=0.8)]))

    await queue_voice(session)
    await process(session)

    (body,) = await reply_bodies(session)
    assert body.startswith("[debug] language=pa | intent=price")


async def test_t11_unclear_punjabi_gets_a_clarification(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch)
    use_voice(monkeypatch, FakeSTT([fake_result(PUNJABI, language="pa", confidence=0.2)]))

    inbox_id = await queue_voice(session)
    await process(session)

    # a new conversation has no language yet: not English -> Roman Urdu (owner, 2026-10-18)
    expected = clarification_message(GateReason.LOW_CONFIDENCE, "roman_ur")
    assert await reply_bodies(session) == [expected]
    assert await inbox_status(session, inbox_id) == "done"


async def test_without_the_debug_switch_a_voice_note_gets_the_same_reply_as_text(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch)
    use_voice(monkeypatch, FakeSTT([ENGLISH]))

    await queue_voice(session)
    await process(session)

    # the empty FakeLLM router answers 'unclear': the graph asks, exactly as for typed text.
    # The transcript is English, so the question is in English (not the en_roman_ur mix).
    assert await reply_bodies(session) == [render("ask_detail", "en")]


# ---- T22: a doubtful transcript is never answered


async def test_t22_a_doubtful_transcript_is_not_kept_and_not_answered(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch, debug_reply=True)
    llm = FakeLLM([])  # one call would raise: the router must not be asked
    monkeypatch.setattr(worker, "get_chat_model", lambda role="router": llm)
    use_voice(monkeypatch, FakeSTT([fake_result("iski price kya hai", confidence=0.1)]))

    await queue_voice(session)
    await process(session)

    assert llm.calls == []
    assert await reply_bodies(session) == [
        clarification_message(GateReason.LOW_CONFIDENCE, "roman_ur")
    ]
    assert await kept_messages(session) == [("in", "[voice: unclear]")]  # no doubtful text kept


async def test_the_clarification_follows_the_conversation_language(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch)
    use_voice(monkeypatch, FakeSTT([fake_result("iski price kya hai", confidence=0.1)]))

    state = await repo.load_state(session, await conversation_id(session))
    state.language = Lang.ROMAN_UR
    await repo.save_state(session, await conversation_id(session), state)

    await queue_voice(session)
    await process(session)

    (body,) = await reply_bodies(session)
    assert body == clarification_message(GateReason.LOW_CONFIDENCE, "roman_ur")
    assert "\n" not in body  # one line, Roman Urdu only


# ---- limits


async def test_a_too_long_voice_note_is_refused_before_transcription(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch)
    stt = FakeSTT([ENGLISH])
    events = use_voice(monkeypatch, stt, duration_s=91.0)

    await queue_voice(session)
    await process(session)

    (body,) = await reply_bodies(session)
    # no language yet: Roman Urdu (owner, 2026-10-18)
    assert body == clarification_message(GateReason.TOO_LONG, "roman_ur")
    assert "lambi" in body
    assert stt.calls == []
    assert events == ["open", "closed"]


async def test_a_file_too_big_for_the_download_is_refused(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch)
    stt = FakeSTT([ENGLISH])
    use_voice(monkeypatch, stt, error=MediaTooLarge("big"))

    await queue_voice(session)
    await process(session)

    (body,) = await reply_bodies(session)
    assert body == clarification_message(GateReason.TOO_LARGE, "roman_ur")
    assert stt.calls == []


# ---- T25: what is not a voice note


async def test_t25_a_media_file_that_is_not_audio_gets_the_polite_reply(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch)
    use_voice(monkeypatch, FakeSTT([ENGLISH]), error=UnsupportedMedia("image/jpeg"))

    inbox_id = await queue_voice(session)
    await process(session)

    assert await reply_bodies(session) == [worker.UNSUPPORTED_REPLY]
    assert await inbox_status(session, inbox_id) == "done"


async def test_t25_audio_without_a_media_id_gets_the_polite_reply(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch)
    stt = FakeSTT([ENGLISH])
    use_voice(monkeypatch, stt)

    await queue_voice(session, media_id=None)
    await process(session)

    assert await reply_bodies(session) == [worker.UNSUPPORTED_REPLY]
    assert stt.calls == []


# ---- provider failure: the message is retried, nothing is half-done


async def test_a_provider_failure_is_raised_so_the_worker_can_retry(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch)
    events = use_voice(monkeypatch, FakeSTT([TranscriptionError("boom")]))

    await queue_voice(session)
    inbox = await repo.claim_inbox(session)
    assert inbox is not None
    with pytest.raises(TranscriptionError):
        await worker.handle_inbox(session, inbox)

    assert await reply_bodies(session) == []
    assert await kept_messages(session) == []
    assert events == ["open", "closed"]  # the audio is gone, also after the failure


# ---- /dev/simulate: a voice note with no phone


async def test_a_dev_voice_note_skips_the_download_and_speaks_the_fake_text(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch, stt_fake_text="iski price kya hai", debug_reply=True)
    llm = FakeLLM([router_answer("roman_ur")])
    monkeypatch.setattr(worker, "get_chat_model", lambda role="router": llm)

    def must_not_download(*args: object, **kwargs: object) -> None:
        raise AssertionError("a dev voice note must not be downloaded")

    monkeypatch.setattr(worker, "prepared_audio", must_not_download)

    await queue_voice(session, media_id="dev-media")
    await process(session)

    assert "iski price kya hai" in llm.calls[0][1]
    (body,) = await reply_bodies(session)
    assert body.startswith("[debug] language=roman_ur | intent=price")
    assert await kept_messages(session) == [("in", "iski price kya hai")]


async def test_a_dev_media_id_outside_local_takes_the_real_path(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(monkeypatch, app_env="staging", stt_fake_text="iski price kya hai")
    events: list[str] = []

    @asynccontextmanager
    async def real_path(media_id: str, *, max_bytes: int, max_seconds: int):
        events.append("real path")
        yield PreparedAudio(wav_path=WAV, duration_s=4.0, size_bytes=1, mime_type="audio/ogg")

    monkeypatch.setattr(worker, "prepared_audio", real_path)

    await queue_voice(session, media_id="dev-media")
    await process(session)

    assert events == ["real path"]
