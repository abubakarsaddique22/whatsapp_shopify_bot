"""Phase 12, Step 5: the failure drills (Blueprint 11.2: Shopify down, LLM down, STT down, Meta send
failing, database restart). The written expected behaviour of every drill is in
src/app/evals/drills.py; each test name below is listed there.

Run them with:  uv run python scripts\\failure_drills.py   (report)  or  uv run pytest tests\\drills

What is real: the worker (`process_inbox`, `handle_inbox`, `send_outbox`), the graph, the
permission gate, the tools, the validator, the handoff service and Postgres. What is broken on
purpose: only the outside world (Shopify, the LLM, the speech provider, Meta, the database
connection). Every test is rolled back (tests/drills/conftest.py).

A test marked `xfail(strict=True)` is a KNOWN GAP: it describes the wanted behaviour and fails
today. When the code is fixed it turns into a failure; then remove the marker and the gap note in
drills.py.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import inspect
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import worker
from app.api import whatsapp_webhook
from app.catalog import stock as stock_mod
from app.catalog.importer import sync_product
from app.core.errors import (
    ProviderError,
    ProviderUnavailable,
    ShopifyUnavailable,
    TranscriptionError,
    WhatsAppError,
    WhatsAppTemporaryError,
)
from app.core.security import sign_whatsapp
from app.core.settings import Settings
from app.db import repositories as repo
from app.db.session import get_session
from app.db.tables import WhatsAppInbox, WhatsAppOutbox
from app.domain.catalog import Variant
from app.integrations.speech.fake_stt import FakeSTT
from app.integrations.whatsapp import sender
from app.integrations.whatsapp.payloads import InboundMessage
from app.voice.convert import PreparedAudio
from tests.adversarial.worker_kit import Chat, any_lang
from tests.factories import make_product, make_variant

PID = "gid://shopify/Product/9990777"
VID = "gid://shopify/ProductVariant/99907771"  # the variant make_product() builds for PID
TITLE = "Drillwatch Kestrel QX9"
MIRROR_PRICE = re.compile(r"12[,.]?500")  # make_variant's price: must never be said while down

HONEST_NO_PRICE = (
    "Maaf kijiye, is waqt is ghari ki price check nahi ho pa rahi. Thori der baad dobara poochein."
)
INVENTED_PRICE = "Is ghari ki price Rs 12,500 hai."

WEBHOOK_SECRET = "drill-app-secret"
VOICE_SETTINGS: dict[str, Any] = {
    "app_env": "local",
    "stt_provider": "fake",
    "stt_fake_text": "",
    "stt_min_words": 2,
    "stt_min_confidence": 0.5,
    "stt_allowed_languages": "en,ur,pa",
    "stt_language_hint": "",
    "max_audio_seconds": 90,
    "max_audio_bytes": 5_000_000,
    "urdu_script_replies": False,
}


# ---- helpers


def router_price() -> str:
    return json.dumps(
        {"language": "roman_ur", "intent": "price", "confidence": 0.9, "product_query": TITLE}
    )


def composer_says(text: str) -> str:
    return json.dumps({"detected_language": "roman_ur", "intent": "price", "response_text": text})


async def add_drill_product(db: AsyncSession) -> None:
    """One visible product with one variant in the mirror (rolled back with the test)."""
    product = make_product(
        PID,
        title=TITLE,
        handle="drillwatch-kestrel-qx9",
        url="https://example.com/products/drillwatch-kestrel-qx9",
    )
    await sync_product(db, PID, product)


def use_shopify(monkeypatch: pytest.MonkeyPatch, *, down: bool) -> list[str]:
    """The live variant lookup: down raises ShopifyUnavailable, up returns the drill variant."""
    stock_mod.clear_live_cache()
    calls: list[str] = []

    async def get_variant(variant_id: str) -> Variant | None:
        calls.append(variant_id)
        if down:
            raise ShopifyUnavailable("Shopify is down (drill)")
        return make_variant(id=VID, product_id=PID)

    monkeypatch.setattr(stock_mod.gateway, "get_variant", get_variant)
    return calls


async def inbox_row(db: AsyncSession, inbox_id: int) -> Any:
    result = await db.execute(
        select(WhatsAppInbox.status, WhatsAppInbox.attempts, WhatsAppInbox.last_error).where(
            WhatsAppInbox.id == inbox_id
        )
    )
    return result.one()


async def outbox_rows(db: AsyncSession, chat: Chat) -> list[Any]:
    result = await db.execute(
        select(WhatsAppOutbox.status, WhatsAppOutbox.attempts, WhatsAppOutbox.last_error)
        .where(WhatsAppOutbox.conversation_id == await chat.conversation_id())
        .order_by(WhatsAppOutbox.id)
    )
    return list(result.all())


async def run_inbox(maker: Any, rounds: int) -> None:
    for _ in range(rounds):
        if not await worker.process_inbox(maker):
            return


async def drain_outbox(maker: Any, rounds: int = 20) -> None:
    for _ in range(rounds):
        if not await worker.send_outbox(maker):
            return


# ---- drill 1: Shopify down


async def test_shopify_down_price_is_not_guessed(
    chat: Chat, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_drill_product(db)
    lookups = use_shopify(monkeypatch, down=True)
    llm = chat.use_model(router_price(), composer_says(HONEST_NO_PRICE))

    inbox_id, _ = await chat.say(f"{TITLE} ki price kya hai?")

    assert lookups == [VID]  # the live lookup was tried, Shopify failed
    assert await chat.status(inbox_id) == "done"
    assert len(llm.calls) >= 2
    assert "shopify_unavailable" in llm.calls[1][1]  # the composer was told the truth
    (body,) = await chat.bodies()
    assert body in {HONEST_NO_PRICE} | any_lang("safe_fallback")
    assert not MIRROR_PRICE.search(body)  # the mirror price is never used as a fallback


async def test_shopify_down_an_invented_price_never_reaches_the_customer(
    chat: Chat, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_drill_product(db)
    use_shopify(monkeypatch, down=True)
    lie = composer_says(INVENTED_PRICE)
    llm = chat.use_model(router_price(), lie, lie)  # the composer invents a price twice

    inbox_id, _ = await chat.say(f"{TITLE} ki price kya hai?")

    assert len(llm.calls) == 3  # router, composer, the one retry
    assert await chat.status(inbox_id) == "done"
    (body,) = await chat.bodies()
    assert body in any_lang("safe_fallback")
    assert not MIRROR_PRICE.search(body)
    assert [h.reason for h in await chat.handoffs()] == ["validator_rejected"]


# ---- drill 2: LLM down


async def test_llm_composer_down_gives_the_safe_reply_and_a_handoff(
    chat: Chat, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_drill_product(db)
    use_shopify(monkeypatch, down=False)
    chat.use_model(router_price(), ProviderUnavailable("composer provider down (drill)"))

    inbox_id, outcome = await chat.say(f"{TITLE} ki price kya hai?")

    assert await chat.status(inbox_id) == "done"
    (body,) = await chat.bodies()
    assert body in any_lang("safe_fallback")
    assert [h.reason for h in await chat.handoffs()] == ["composer_failed"]
    assert outcome.new_handoff  # staff are told after the commit
    state = await chat.state()
    assert state.needs_handoff and state.bot_paused_until is not None


async def test_llm_router_down_says_so_and_hands_off(chat: Chat) -> None:
    llm = chat.use_model(ProviderUnavailable("router provider down (drill)"))

    inbox_id, outcome = await chat.say("is ghari ki price kya hai?")

    assert len(llm.calls) == 1  # no repair call after a provider error, no composer call
    assert await chat.status(inbox_id) == "done"
    (body,) = await chat.bodies()
    assert body in any_lang("bot_trouble")  # honest: a technical problem, not 'unclear'
    assert body not in any_lang("ask_detail")
    assert [h.reason for h in await chat.handoffs()] == ["llm_unavailable"]
    assert outcome.new_handoff
    state = await chat.state()
    assert state.needs_handoff and state.bot_paused_until is not None


async def test_llm_router_down_next_messages_wait_for_the_team(chat: Chat) -> None:
    for _ in range(3):
        chat.use_model(ProviderUnavailable("router provider down (drill)"))
        await chat.say("is ghari ki price kya hai?")

    bodies = await chat.bodies()
    assert len(bodies) == 2  # the honest reply, ONE 'team has your request', then silence
    assert bodies[0] in any_lang("bot_trouble")
    assert bodies[1] in any_lang("paused_ack")
    assert len(await chat.handoffs()) == 1  # one ticket for staff, not one per message
    assert len(await chat.kept_messages()) == 3  # staff can read every message


async def test_llm_a_garbled_answer_is_still_treated_as_unclear(chat: Chat) -> None:
    """The boundary of the fix: only a provider OUTAGE is our fault. A provider that answers,
    but with nothing usable (plain ProviderError, two invalid answers), still gets the normal
    clarifying question."""
    chat.use_model(ProviderError("no usable answer (drill)"))

    await chat.say("is ghari ki price kya hai?")

    (body,) = await chat.bodies()
    assert body in any_lang("ask_detail")
    assert await chat.handoffs() == []


# ---- drill 3: STT down


def use_dead_stt(chat: Chat, monkeypatch: pytest.MonkeyPatch, tries: int) -> list[str]:
    """Speech provider fails every time; the download is a stand-in that records open/closed."""
    chat.use_settings(**VOICE_SETTINGS)
    stt = FakeSTT([TranscriptionError("speech provider down (drill)") for _ in range(tries)])
    events: list[str] = []

    @asynccontextmanager
    async def fake_prepared_audio(
        media_id: str, *, max_bytes: int, max_seconds: int
    ) -> AsyncIterator[PreparedAudio]:
        events.append("open")
        try:
            yield PreparedAudio(
                wav_path=Path("note.wav"),
                duration_s=4.0,
                size_bytes=50_000,
                mime_type="audio/ogg",
            )
        finally:
            events.append("closed")

    monkeypatch.setattr(worker, "get_stt_provider", lambda settings=None: stt)
    monkeypatch.setattr(worker, "prepared_audio", fake_prepared_audio)
    return events


async def queue_voice_note(db: AsyncSession, chat: Chat) -> int:
    msg = InboundMessage(
        wamid=f"wamid.drill-{uuid.uuid4().hex[:12]}",
        wa_id=chat.wa_id,
        msg_type="audio",
        media_id="MEDIA1",
        mime_type="audio/ogg; codecs=opus",
    )
    inbox_id = await repo.add_inbox(db, await chat.conversation_id(), chat.wa_hash, msg)
    assert inbox_id is not None
    return inbox_id


async def test_stt_down_is_retried_then_the_customer_is_told(
    chat: Chat, db: AsyncSession, maker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = use_dead_stt(chat, monkeypatch, tries=worker.MAX_ATTEMPTS)
    inbox_id = await queue_voice_note(db, chat)

    await run_inbox(maker, rounds=worker.MAX_ATTEMPTS + 2)

    row = await inbox_row(db, inbox_id)
    assert row.status == "done"
    assert row.attempts == worker.MAX_ATTEMPTS - 1  # two failed tries, the last one answers
    assert row.last_error == "speech provider down (drill)"  # the kind of error, nothing else
    (body,) = await chat.bodies()  # exactly one honest reply, nothing from the failed tries
    assert body in any_lang("voice_unavailable")
    assert await chat.kept_messages() == ["[voice: not transcribed]"]  # no transcript, no audio
    assert events == ["open", "closed"] * worker.MAX_ATTEMPTS  # the audio is gone every time


async def test_stt_recovers_on_the_retry(
    chat: Chat, db: AsyncSession, maker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_dead_stt(chat, monkeypatch, tries=0)
    blip = TranscriptionError("speech provider blip (drill)")
    stt = FakeSTT([blip, "is ghari ki price batayein"])  # fails once, then works
    monkeypatch.setattr(worker, "get_stt_provider", lambda settings=None: stt)
    inbox_id = await queue_voice_note(db, chat)

    await run_inbox(maker, rounds=worker.MAX_ATTEMPTS + 2)

    row = await inbox_row(db, inbox_id)
    assert row.status == "done" and row.attempts == 1
    (body,) = await chat.bodies()  # the normal turn (empty FakeLLM: a clarifying question)
    assert body not in any_lang("voice_unavailable")
    assert await chat.kept_messages() == ["is ghari ki price batayein"]


# ---- drill 4: Meta send failing


class FakeMetaSend:
    """Stands in for client.send_text: raises the prepared errors in order, then accepts."""

    def __init__(self, errors: list[Exception]) -> None:
        self.errors = list(errors)
        self.calls = 0
        self.accepted = 0

    async def send_text(self, to: str, body: str) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        self.accepted += 1
        return f"wamid.drill-out-{uuid.uuid4().hex[:12]}"


def meta_5xx() -> WhatsAppTemporaryError:
    return WhatsAppTemporaryError("HTTP 503", detail={"meta_code": None})


async def queue_one_reply(chat: Chat) -> None:
    """The customer writes, the worker queues one reply (ask_detail: empty FakeLLM)."""
    await chat.say("hmm")
    assert len(await chat.bodies()) == 1


async def test_meta_temporary_error_is_retried_then_dead_lettered(
    chat: Chat, db: AsyncSession, maker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await queue_one_reply(chat)
    meta = FakeMetaSend([meta_5xx() for _ in range(50)])
    monkeypatch.setattr(sender, "send_text", meta.send_text)

    await drain_outbox(maker)

    assert meta.calls == sender.MAX_ATTEMPTS
    assert meta.accepted == 0
    (row,) = await outbox_rows(db, chat)
    assert row.status == "failed" and row.attempts == sender.MAX_ATTEMPTS
    assert "HTTP 503" in row.last_error
    assert chat.wa_id not in row.last_error  # no phone number in the error


async def test_meta_permanent_error_fails_at_once(
    chat: Chat, db: AsyncSession, maker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await queue_one_reply(chat)
    expired = WhatsAppError("HTTP 401", detail={"meta_code": 190})
    meta = FakeMetaSend([expired, expired])
    monkeypatch.setattr(sender, "send_text", meta.send_text)

    await drain_outbox(maker)

    assert meta.calls == 1  # never retried
    (row,) = await outbox_rows(db, chat)
    assert row.status == "failed" and row.attempts == 1
    assert "meta code 190" in row.last_error
    assert chat.wa_id not in row.last_error


async def test_meta_recovers_and_sends_exactly_once(
    chat: Chat, db: AsyncSession, maker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await queue_one_reply(chat)
    meta = FakeMetaSend([meta_5xx()])
    monkeypatch.setattr(sender, "send_text", meta.send_text)

    await drain_outbox(maker)

    assert meta.calls == 2 and meta.accepted == 1
    (row,) = await outbox_rows(db, chat)
    assert row.status == "sent"


# ---- drill 5: database restart


class DeadSession:
    """A session whose database is gone: every query fails like a refused connection."""

    async def execute(self, *args: object, **kwargs: object) -> Any:
        raise ConnectionRefusedError("database is restarting (drill)")

    async def get(self, *args: object, **kwargs: object) -> Any:
        raise ConnectionRefusedError("database is restarting (drill)")

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class DeadMaker:
    def __call__(self) -> "DeadMaker":
        return self

    async def __aenter__(self) -> DeadSession:
        return DeadSession()

    async def __aexit__(self, *exc: object) -> None:
        return None


def webhook_body(wa_id: str, wamid: str, text: str) -> bytes:
    message = {
        "from": wa_id,
        "id": wamid,
        "timestamp": "1760169600",
        "type": "text",
        "text": {"body": text},
    }
    value = {"messaging_product": "whatsapp", "messages": [message]}
    body = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": value}]}]}
    return json.dumps(body).encode()


async def test_database_down_webhook_is_refused_so_meta_retries(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        whatsapp_app_secret=SecretStr(WEBHOOK_SECRET),
        whatsapp_verify_token=SecretStr("drill-verify"),
        phone_hash_pepper=SecretStr("drill-pepper"),
    )
    monkeypatch.setattr(whatsapp_webhook, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(whatsapp_webhook.router)
    database_up = False

    async def session_dependency() -> AsyncIterator[AsyncSession]:
        if not database_up:
            raise ConnectionRefusedError("database is restarting (drill)")
        yield db

    app.dependency_overrides[get_session] = session_dependency
    wamid = f"wamid.drill-{uuid.uuid4().hex[:12]}"
    raw = webhook_body("920000000056", wamid, "price kya hai?")
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sign_whatsapp(raw, WEBHOOK_SECRET),
    }
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://drill") as client:
        refused = await client.post("/webhook/whatsapp", content=raw, headers=headers)
        assert refused.status_code >= 500  # not 200: Meta keeps the message and delivers again

        database_up = True
        for _ in range(2):  # Meta's redelivery, twice
            answer = await client.post("/webhook/whatsapp", content=raw, headers=headers)
            assert answer.status_code == 200

    stored = await db.execute(
        select(func.count()).select_from(WhatsAppInbox).where(WhatsAppInbox.wamid == wamid)
    )
    assert stored.scalar_one() == 1  # stored once, nothing lost


async def test_database_down_worker_waits_and_the_message_stays_pending(
    chat: Chat, maker: Any
) -> None:
    inbox_id = await chat.receive("hmm")

    with pytest.raises(ConnectionRefusedError):
        await worker.process_inbox(DeadMaker())  # main() catches it: worker_loop_error, wait
    assert await worker.send_outbox(DeadMaker()) is False  # the sender pass returns quietly
    assert "worker_loop_error" in inspect.getsource(worker.main)
    assert await chat.status(inbox_id) == "pending"

    assert await worker.process_inbox(maker)  # the database is back
    assert await chat.status(inbox_id) == "done"
    assert len(await chat.bodies()) == 1


async def test_database_lost_mid_turn_leaves_nothing_half_done(
    chat: Chat, db: AsyncSession, maker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_add_message = repo.add_message
    lost = {"done": False}

    async def add_message_connection_lost(*args: Any, **kwargs: Any) -> None:
        if not lost["done"]:
            lost["done"] = True
            raise ConnectionResetError("connection to the database was lost (drill)")
        await real_add_message(*args, **kwargs)

    monkeypatch.setattr(repo, "add_message", add_message_connection_lost)
    inbox_id = await chat.receive("hmm")

    assert await worker.process_inbox(maker)
    row = await inbox_row(db, inbox_id)
    assert row.status == "pending" and row.attempts == 1
    assert await chat.bodies() == []  # the reply of the lost turn was rolled back
    assert await chat.kept_messages() == []

    assert await worker.process_inbox(maker)  # the retry after the restart
    assert (await inbox_row(db, inbox_id)).status == "done"
    assert len(await chat.bodies()) == 1  # exactly one reply
    assert await chat.kept_messages() == ["hmm"]
