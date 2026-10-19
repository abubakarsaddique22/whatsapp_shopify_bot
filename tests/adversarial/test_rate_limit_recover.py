"""Rate limits through the real worker: a flood is stopped, and the customer is NOT locked out
for ever (Blueprint 9.6).

* over a limit: ONE polite message, then silence, and the silent messages cost nothing;
* when the window has passed, the bot answers again (minute, hour and day limits);
* three attacks in an hour start a cool-down (one notice, then silence); it ends by itself;
* one noisy customer never slows down another customer;
* the daily budget comes back when old messages leave the 24 hour window.

Time is moved with `Chat.age()` (see worker_kit.py), no sleeping.
Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import pytest
from sqlalchemy import select

from app.db.tables import SecurityEvent
from tests.adversarial.harness import router_says
from tests.adversarial.worker_kit import ATTACK, Chat, any_lang

# (the limit that is set to 1, how far back the old messages move so the window is over)
WINDOWS = [
    pytest.param("rate_limit_per_minute", {"minutes": 2}, id="minute"),
    pytest.param("rate_limit_per_hour", {"minutes": 61}, id="hour"),
    pytest.param("rate_limit_per_day", {"hours": 25}, id="day"),
]


@pytest.mark.parametrize(("limit", "wait"), WINDOWS)
async def test_one_notice_then_silence_then_the_bot_answers_again(
    chat: Chat, limit: str, wait: dict
) -> None:
    chat.use_settings(**{limit: 1})

    first, _ = await chat.say()
    second, _ = await chat.say()
    third, _ = await chat.say()

    bodies = await chat.bodies()
    assert len(bodies) == 2  # the answer to the first message, ONE polite notice
    assert bodies[1] in any_lang("rate_limited")
    assert await chat.status(first) == "done"
    assert await chat.status(second) == "ignored"  # costs nothing
    assert await chat.status(third) == "ignored"  # silence, no second notice

    await chat.age(**wait)  # the window is over
    fourth, _ = await chat.say()

    assert await chat.status(fourth) == "done"
    assert (await chat.bodies())[-1] in any_lang("ask_detail")  # a normal answer again
    assert len(await chat.bodies()) == 3


async def test_a_second_flood_after_the_recovery_gets_a_new_notice(chat: Chat) -> None:
    chat.use_settings(rate_limit_per_minute=1)
    await chat.say()
    await chat.say()  # over the limit: notice
    await chat.age(minutes=2)
    await chat.say()  # recovered: a normal answer

    await chat.say()  # over the limit again

    assert (await chat.bodies())[-1] in any_lang("rate_limited")
    assert len(await chat.bodies()) == 4


async def test_a_noisy_customer_does_not_slow_down_another_one(
    chat: Chat, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat.use_settings(rate_limit_per_minute=1)
    other = Chat(db, monkeypatch, wa_id="920000000023", wa_hash="testhash-adv2")
    other.use_settings(rate_limit_per_minute=1)
    await chat.say()
    noisy, _ = await chat.say()
    assert await chat.status(noisy) == "ignored"

    quiet, _ = await other.say()

    assert await other.status(quiet) == "done"
    assert (await other.bodies())[-1] in any_lang("ask_detail")


# ---- strikes: attacks start a cool-down that ends by itself


async def test_three_attacks_start_a_cool_down_that_ends_by_itself(chat: Chat) -> None:
    chat.use_model(*[router_says("abuse_or_injection")] * 3)

    for _ in range(3):
        inbox_id, _ = await chat.say(ATTACK)
        assert await chat.status(inbox_id) == "done"  # a fixed refusal each time
    assert await chat.events("injection_suspected") == 3  # the strikes, written by the graph
    refusals = await chat.bodies()
    assert set(refusals) <= any_lang("abuse_or_injection")

    fourth, _ = await chat.say(ATTACK)  # the third strike was the last free one
    fifth, _ = await chat.say(ATTACK)

    assert (await chat.bodies())[3] in any_lang("cooldown")  # ONE notice
    assert len(await chat.bodies()) == 4  # then silence
    assert await chat.status(fourth) == "ignored"
    assert await chat.status(fifth) == "ignored"
    assert len(chat.llm.calls) == 3  # the cool-down messages never reached the model

    await chat.age(hours=2)  # the strikes are older than an hour
    sixth, _ = await chat.say("hmm")

    assert await chat.status(sixth) == "done"
    assert (await chat.bodies())[-1] in any_lang("ask_detail")


async def test_attacks_are_stored_without_the_message_text(chat: Chat) -> None:
    chat.use_model(router_says("abuse_or_injection"))

    await chat.say(ATTACK)

    rows = await chat.session.execute(
        select(SecurityEvent.kind, SecurityEvent.detail, SecurityEvent.ref).where(
            SecurityEvent.wa_hash == chat.wa_hash
        )
    )
    stored = str(rows.all())
    assert "injection_suspected" in stored
    assert "previous instructions" not in stored
    assert chat.wa_id not in stored


# ---- the daily budget comes back


async def test_the_daily_budget_comes_back_when_old_messages_leave_the_window(
    chat: Chat,
) -> None:
    chat.use_settings(daily_cost_budget=2.0, cost_per_text_message=1.0)

    first, _ = await chat.say("hmm")  # 1 of 2: a normal answer
    second, outcome = await chat.say("hmm")  # 2 of 2: handoff-only, a person takes over
    third, _ = await chat.say("hmm")  # still used up: silence

    assert await chat.status(first) == "done"
    assert await chat.status(second) == "ignored"
    assert await chat.status(third) == "ignored"
    bodies = await chat.bodies()
    assert bodies[1] in any_lang("handoff_created")
    assert len(bodies) == 2  # told once a day, then silence
    assert outcome.new_handoff is True
    assert [row.reason for row in await chat.handoffs()] == ["budget_reached"]

    await chat.age(hours=25)  # the spend of the last 24 hours is small again
    fourth, _ = await chat.say("hmm")

    assert await chat.status(fourth) == "done"
    assert (await chat.bodies())[-1] in any_lang("ask_detail")
