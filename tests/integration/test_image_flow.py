"""The image request end to end: T05, T23, T24 on the real Postgres (Phase 7, updated in Phase 9).

A customer text goes through the REAL worker (the LangGraph workflow: router, gate, tools,
composer, validator, outbox) and the REAL sender code. Only the outside world is fake: the model
answers come from ONE FakeLLM (the router answer first, then the composer answer when the graph
needs one), and the WhatsApp calls (send_image, fetch_and_prepare, send_text) are replaced, so no
phone, no token and no network are needed. Made-up products ('Qorvex', 'Nimbra', 'Zorbex') are
inside a transaction that is rolled back, so the real mirror and outbox rows are never touched.

Since Phase 9 the pictures still come from the picture tool (never from the model), but the
clarifying questions are templates in the conversation language and a reply without a picture is
written by the composer, so those tests compare with render() and a composer answer.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import worker
from app.agent.templates import render
from app.catalog.importer import compute_hash, image_rows, product_row, variant_rows
from app.core.errors import WhatsAppError, WhatsAppTemporaryError
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.db.tables import WhatsAppInbox, WhatsAppOutbox
from app.domain.catalog import Image, Money, Product, StockState, Variant
from app.integrations.llm.fake_llm import FakeLLM
from app.integrations.whatsapp import sender
from app.integrations.whatsapp.payloads import InboundMessage
from app.language.policy import reply_language

WA_ID = "920000000011"
REEF_LINK = "https://shop.example/products/qorvex-reef"
NIMBRA_LINK = "https://shop.example/products/nimbra-bare"
REEF_BLUE_VARIANT_PICTURE = "https://cdn.example/reef-blue.jpg?v=7"
REEF_FEATURED = "https://cdn.example/reef-1.jpg?v=1"
FALLBACK = f"Qorvex Reef: {REEF_LINK}"
REFUSED = WhatsAppError("HTTP 400", detail={"meta_code": 131053})

# What the composer (FakeLLM) writes. Plain Roman Urdu, no numbers, no colours, no other links.
PICTURE_TEXT = "Ji, yeh aap ki photo hai."
NO_PICTURE_TEXT = (
    f"Maazrat, is watch ki photo mojood nahi hai. Aap yahan dekh sakte hain: {NIMBRA_LINK}"
)


def make_variant(product_no: int, n: int, colour: str, image: str | None = None) -> Variant:
    return Variant(
        id=f"gid://shopify/ProductVariant/{product_no}0{n}",
        product_id=f"gid://shopify/Product/{product_no}",
        title=f"{colour} / 40mm",
        price=Money(amount=Decimal("9000"), currency="PKR"),
        options={"Colour": colour, "Case size": "40mm"},
        available_for_sale=True,
        inventory_quantity=3,
        stock=StockState.IN_STOCK,
        image_url=image,
    )


def make_product(
    no: int,
    title: str,
    variants: list[Variant],
    images: list[Image] | None = None,
    vendor: str = "",
) -> Product:
    return Product(
        id=f"gid://shopify/Product/{no}",
        title=title,
        handle=title.lower().replace(" ", "-"),
        url=f"https://shop.example/products/{title.lower().replace(' ', '-')}",
        vendor=vendor,
        images=images or [],
        variants=variants,
    )


REEF = make_product(  # black has no picture of its own, blue has one
    9301,
    "Qorvex Reef",
    [make_variant(9301, 1, "Black"), make_variant(9301, 2, "Blue", REEF_BLUE_VARIANT_PICTURE)],
    images=[
        Image(url=REEF_FEATURED, alt="front"),
        Image(url="https://cdn.example/reef-blue.jpg?v=5"),
        Image(url="https://cdn.example/reef-3.jpg"),
        Image(url="https://cdn.example/reef-4.jpg"),
    ],
)
BARE = make_product(9302, "Nimbra Bare", [make_variant(9302, 1, "Black")])  # no pictures at all
TIDE = make_product(9303, "Zorbex Tide", [make_variant(9303, 1, "Black")], vendor="Zorbex")
TIDE_ULTRA = make_product(
    9304, "Zorbex Tide Ultra", [make_variant(9304, 1, "Blue")], vendor="Zorbex"
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            db = AsyncSession(bind=conn, expire_on_commit=False)
            await db.execute(delete(WhatsAppOutbox))
            await db.execute(delete(WhatsAppInbox))
            for product in (REEF, BARE, TIDE, TIDE_ULTRA):
                await repo.upsert_product(
                    db,
                    product_row(product, compute_hash(product)),
                    variant_rows(product),
                    image_rows(product),
                )
            yield db
            await db.close()
            await transaction.rollback()
    finally:
        await engine.dispose()


# ---- helpers


def router_says(**fields: Any) -> str:
    """What the router (FakeLLM) answers: an image_request unless the test says otherwise."""
    data = {"language": "roman_ur", "intent": "image_request", "confidence": 0.9, **fields}
    return json.dumps(data)


def composer_says(text: str = PICTURE_TEXT) -> str:
    """What the composer answers after the tools ran (the graph overrides intent and language)."""
    return json.dumps(
        {"detected_language": "roman_ur", "intent": "image_request", "response_text": text}
    )


def use_worker(monkeypatch: pytest.MonkeyPatch, *answers: str) -> FakeLLM:
    """One FakeLLM for both roles: per message the router answer comes first, then the composer."""
    llm = FakeLLM(list(answers))
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: Settings(kill_switch=False, debug_reply=False, urdu_script_replies=False),
    )
    monkeypatch.setattr(worker, "get_chat_model", lambda role="router": llm)
    return llm


async def say(session: AsyncSession, text: str) -> None:
    """One customer message through the worker (without its own commit)."""
    conversation = await repo.get_or_create_conversation(session, WA_ID, "testhash")
    message = InboundMessage(
        wamid=f"wamid.test-{uuid.uuid4().hex[:12]}", wa_id=WA_ID, msg_type="text", text=text
    )
    assert await repo.add_inbox(session, conversation, "testhash", message) is not None
    inbox = await repo.claim_inbox(session)
    assert inbox is not None
    await worker.handle_inbox(session, inbox)


async def outbox(session: AsyncSession) -> list[Any]:
    result = await session.execute(
        select(
            WhatsAppOutbox.kind,
            WhatsAppOutbox.status,
            WhatsAppOutbox.attempts,
            WhatsAppOutbox.media_url,
            WhatsAppOutbox.caption,
            WhatsAppOutbox.body,
            WhatsAppOutbox.last_error,
        ).order_by(WhatsAppOutbox.id)
    )
    return list(result.all())


async def state_of_conversation(session: AsyncSession) -> Any:
    conversation = await repo.get_or_create_conversation(session, WA_ID, "testhash")
    return await repo.load_state(session, conversation)


async def template(session: AsyncSession, kind: str, **values: str) -> str:
    """The template text in the language the conversation ended up with."""
    state = await state_of_conversation(session)
    return render(kind, reply_language(state.language, urdu_script_replies=False), **values)


def fake_send_image(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[str | Exception]
) -> list[dict[str, Any]]:
    """Each call returns (or raises) the next outcome. Returns the recorded calls."""
    calls: list[dict[str, Any]] = []

    async def fake(
        to: str, *, link: str | None = None, media_id: str | None = None, caption: str | None = None
    ) -> str:
        calls.append({"link": link, "media_id": media_id, "caption": caption})
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sender, "send_image", fake)
    return calls


def fake_fetch_fails(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    async def fake(url: str, **_: Any) -> tuple[bytes, str]:
        raise error

    monkeypatch.setattr(sender, "fetch_and_prepare", fake)


def fake_send_text(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    bodies: list[str] = []

    async def fake(to: str, body: str) -> str:
        bodies.append(body)
        return "wamid.text-out"

    monkeypatch.setattr(sender, "send_text", fake)
    return bodies


# ---- T05: the right picture is sent


async def test_t05_colour_picture_is_queued_and_sent_through_the_fake_client(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(monkeypatch, router_says(product_query="Qorvex Reef", color="blue"))

    await say(session, "blue wali photo bhejo")

    (queued,) = await outbox(session)
    assert (queued.kind, queued.status) == ("image", "pending")
    assert queued.media_url == REEF_BLUE_VARIANT_PICTURE  # the blue variant's own picture
    assert queued.caption == "Qorvex Reef (Blue)"
    assert queued.body == FALLBACK  # the text a failed picture falls back to
    state = await state_of_conversation(session)
    assert state.current_product_id == REEF.id

    calls = fake_send_image(monkeypatch, ["wamid.img"])
    assert await sender.send_one(session) is True

    expected = {
        "link": REEF_BLUE_VARIANT_PICTURE,
        "media_id": None,
        "caption": "Qorvex Reef (Blue)",
    }
    assert calls == [expected]
    (sent,) = await outbox(session)
    assert (sent.kind, sent.status) == ("image", "sent")


async def test_t05_colour_without_its_own_picture_gets_the_main_one_and_says_so(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(monkeypatch, router_says(product_query="Qorvex Reef", color="black"))

    await say(session, "black wali photo bhejo")

    (queued,) = await outbox(session)
    assert queued.kind == "image"
    assert queued.media_url == REEF_FEATURED
    missing = await template(session, "picture_colour_missing", title="Qorvex Reef", colour="black")
    assert queued.caption == missing  # no false claim about the colour, in the reply language


async def test_more_pictures_queues_up_to_three_without_repeats(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(monkeypatch, router_says(product_query="Qorvex Reef", color="blue"))

    await say(session, "blue ki aur pictures dikhao")

    rows = await outbox(session)
    assert [r.kind for r in rows] == ["image", "image", "image"]
    urls = [r.media_url for r in rows]
    assert urls[0] == REEF_BLUE_VARIANT_PICTURE
    assert len({u.split("?", 1)[0] for u in urls}) == 3


async def test_a_follow_up_without_a_product_name_uses_the_current_product(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(
        monkeypatch,
        router_says(product_query="Qorvex Reef"),
        router_says(product_query=None),  # "aur pictures hain?" names no product
    )

    await say(session, "Qorvex Reef ki photo")
    await say(session, "aur pictures hain?")

    rows = await outbox(session)
    assert [r.kind for r in rows] == ["image"] * 4  # 1 picture, then 3 more of the SAME product
    assert rows[0].body == FALLBACK
    assert all(r.body == FALLBACK for r in rows[1:])


# ---- T23: no picture


async def test_t23_product_without_a_picture_gets_a_reply_with_the_link(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(
        monkeypatch, router_says(product_query="Nimbra Bare"), composer_says(NO_PICTURE_TEXT)
    )

    await say(session, "Nimbra Bare ki photo bhejo")

    (queued,) = await outbox(session)
    assert queued.kind == "text"  # nothing to send as a picture
    assert queued.media_url is None
    assert queued.body == NO_PICTURE_TEXT  # the composer text passed the validator
    assert NIMBRA_LINK in queued.body  # and the customer gets the product link

    bodies = fake_send_text(monkeypatch)
    await sender.send_one(session)
    assert bodies == [queued.body]  # the customer really gets the link


# ---- ambiguous or unknown product: ask, never guess


async def test_ambiguous_product_gets_one_clarifying_question_and_no_picture(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(monkeypatch, router_says(product_query="zorbex tide"))

    await say(session, "zorbex tide ki photo")

    (queued,) = await outbox(session)
    assert queued.kind == "text"
    prefix = (await template(session, "ask_which_product", names="")).split(":")[0]
    assert queued.body.startswith(prefix)
    assert "Zorbex Tide" in queued.body
    assert "Zorbex Tide Ultra" in queued.body
    state = await state_of_conversation(session)
    assert state.clarification_count == 1
    assert state.current_product_id is None  # nothing was guessed


async def test_after_two_questions_in_a_row_a_person_takes_over(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(monkeypatch, *[router_says(product_query="zorbex tide")] * 3)

    for _ in range(3):
        await say(session, "zorbex tide ki photo")

    bodies = [r.body for r in await outbox(session)]
    prefix = (await template(session, "ask_which_product", names="")).split(":")[0]
    assert bodies[0].startswith(prefix)
    assert bodies[1].startswith(prefix)
    assert bodies[2] == await template(session, "handoff_created")  # not a third question
    state = await state_of_conversation(session)
    assert state.clarification_count == 2
    assert state.needs_handoff is True


async def test_no_product_named_and_none_current_asks_which_one(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(monkeypatch, router_says(product_query=None))

    await say(session, "photo bhejo")

    assert [r.body for r in await outbox(session)] == [await template(session, "ask_product_name")]


async def test_unknown_product_is_not_found(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(monkeypatch, router_says(product_query="Xylophon Quark"))

    await say(session, "Xylophon Quark ki photo")

    assert [r.body for r in await outbox(session)] == [await template(session, "not_found")]


# ---- T24: the picture cannot be delivered


async def test_t24_refused_picture_that_cannot_be_fetched_becomes_a_text_link_and_is_logged(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use_worker(monkeypatch, router_says(product_query="Qorvex Reef", color="blue"))
    await say(session, "blue wali photo bhejo")
    fake_send_image(monkeypatch, [REFUSED])
    fake_fetch_fails(monkeypatch, WhatsAppError("Product picture not available (HTTP 404)"))
    bodies = fake_send_text(monkeypatch)

    with caplog.at_level(logging.WARNING):
        await sender.send_one(session)  # the picture fails, the row turns into text
        await sender.send_one(session)  # the text link goes out

    assert bodies == [FALLBACK]
    (done,) = await outbox(session)
    assert (done.kind, done.status) == ("text", "sent")
    assert "image fallback" in done.last_error
    assert any(record.getMessage() == "outbox_image_fallback" for record in caplog.records)


async def test_t24_temporary_error_is_retried_once_then_the_text_link_goes_out(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(monkeypatch, router_says(product_query="Qorvex Reef", color="blue"))
    await say(session, "blue wali photo bhejo")
    calls = fake_send_image(
        monkeypatch, [WhatsAppTemporaryError("HTTP 500"), WhatsAppTemporaryError("HTTP 500")]
    )
    fake_fetch_fails(monkeypatch, AssertionError("a temporary error must not start the upload"))
    bodies = fake_send_text(monkeypatch)

    await sender.send_one(session)  # first try fails
    await sender.send_one(session)  # the one retry fails too: the row becomes text
    await sender.send_one(session)  # the text link goes out

    assert len(calls) == 2
    assert bodies == [FALLBACK]
    (done,) = await outbox(session)
    assert (done.kind, done.status) == ("text", "sent")
