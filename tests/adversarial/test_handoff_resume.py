"""Handoff and resume through the real worker (Blueprint 8.6).

The whole life of a handoff: the bot gives up, a person is told, the bot goes quiet, staff (or
the timer) hand the customer back, and the bot answers again. Also: the notification is sent
AFTER the commit, a broken channel never loses a handoff or delays the customer, and staff see
only the end of the customer's number.

Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import worker
from app.core.errors import NotificationError
from app.db import repositories as repo
from app.handoff import notifier
from app.handoff import service as handoff_service
from tests.adversarial.conftest import ADMIN_HEADERS
from tests.adversarial.worker_kit import Chat, any_lang, sessionmaker_of


class Channel:
    """Stands in for notifier.send: records what staff would have read, can be told to fail."""

    def __init__(self) -> None:
        self.sent: list[notifier.Notification] = []
        self.error: Exception | None = None

    async def __call__(self, notification: notifier.Notification, settings=None) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(notification)


@pytest.fixture
def channel(monkeypatch: pytest.MonkeyPatch) -> Channel:
    fake = Channel()
    monkeypatch.setattr(notifier, "send", fake)
    return fake


async def three_unclear(chat: Chat) -> worker.TurnOutcome:
    """The third unclear message in a row goes to a person."""
    outcome = worker.TurnOutcome()
    for _ in range(3):
        _, outcome = await chat.say("hmm")
    return outcome


# ---- the whole life of a handoff


async def test_handoff_pause_resume_and_a_second_handoff(chat: Chat) -> None:
    cid = await chat.conversation_id()

    outcome = await three_unclear(chat)

    assert outcome.new_handoff is True
    assert (await chat.bodies())[-1] in any_lang("handoff_created")
    assert [row.status for row in await chat.handoffs()] == ["open"]
    assert handoff_service.is_paused(await chat.state())

    paused, _ = await chat.say("hello? anyone?")  # the bot is quiet: one notice, then nothing
    again, _ = await chat.say("please answer")
    assert (await chat.bodies())[-1] in any_lang("paused_ack")
    assert len(await chat.bodies()) == 4
    assert await chat.status(paused) == "ignored" and await chat.status(again) == "ignored"
    assert {"hello? anyone?", "please answer"} <= set(await chat.kept_messages())  # staff can read

    resumed = await handoff_service.resume(chat.session, cid)  # staff give the customer back

    assert resumed.was_paused is True and resumed.handoffs_resolved == 1
    state = await chat.state()
    assert not handoff_service.is_paused(state) and state.clarification_count == 0
    assert [row.status for row in await chat.handoffs()] == ["resolved"]

    await three_unclear(chat)  # a fresh start: two questions first, a new handoff only on the third

    bodies = await chat.bodies()
    assert bodies[4] in any_lang("ask_detail") and bodies[5] in any_lang("ask_detail")
    assert bodies[6] in any_lang("handoff_created")
    assert [row.status for row in await chat.handoffs()] == ["resolved", "open"]


async def test_resume_twice_is_harmless(chat: Chat) -> None:
    cid = await chat.conversation_id()
    await three_unclear(chat)

    first = await handoff_service.resume(chat.session, cid)
    second = await handoff_service.resume(chat.session, cid)

    assert first.handoffs_resolved == 1
    assert second.handoffs_resolved == 0 and second.was_paused is False


async def test_the_timer_hands_the_customer_back_by_itself(chat: Chat) -> None:
    cid = await chat.conversation_id()
    await three_unclear(chat)
    conv = await chat.state()
    conv.bot_paused_until = datetime.now(UTC) - timedelta(minutes=1)  # the pause has ended
    await repo.save_state(chat.session, cid, conv)

    inbox_id, _ = await chat.say("hmm")

    assert await chat.status(inbox_id) == "done"  # answered, not silent
    assert (await chat.bodies())[-1] in any_lang("ask_detail")
    assert [row.status for row in await chat.handoffs()] == ["resolved"]
    assert not handoff_service.is_paused(await chat.state())


async def test_staff_can_list_and_resume_through_the_admin_api(
    chat: Chat, admin_client: httpx.AsyncClient
) -> None:
    cid = await chat.conversation_id()
    await three_unclear(chat)

    listed = (await admin_client.get("/admin/handoffs", headers=ADMIN_HEADERS)).json()
    assert [row["conversation_id"] for row in listed].count(cid) == 1
    assert chat.wa_id not in str(listed)  # staff get a summary, never the whole number

    resumed = await admin_client.post(f"/admin/conversations/{cid}/resume", headers=ADMIN_HEADERS)
    assert resumed.status_code == 200 and resumed.json()["handoffs_resolved"] == 1

    inbox_id, _ = await chat.say("hmm")
    assert await chat.status(inbox_id) == "done"
    assert (await chat.bodies())[-1] in any_lang("ask_detail")


# ---- telling staff


async def test_staff_are_told_once_after_the_commit_and_see_only_the_end_of_the_number(
    chat: Chat, channel: Channel
) -> None:
    outcome = await three_unclear(chat)
    assert channel.sent == []  # nothing is sent inside the customer's transaction

    await worker.announce(sessionmaker_of(chat.session), outcome)

    assert len(channel.sent) == 1
    note = channel.sent[0]
    assert "number ending 0021" in note.body and chat.wa_id not in note.body
    assert "hmm" in note.body  # the summary: what the customer said
    (record,) = await chat.handoffs()
    assert f"#{record.id}]" in note.subject
    await worker.announce(sessionmaker_of(chat.session), worker.TurnOutcome())
    assert len(channel.sent) == 1  # no new handoff, no new message


async def test_a_broken_channel_never_loses_the_handoff_or_the_reply(
    chat: Chat, channel: Channel
) -> None:
    channel.error = NotificationError("slack down (HTTP 500)")
    outcome = await three_unclear(chat)

    await worker.announce(sessionmaker_of(chat.session), outcome)  # must not raise

    assert (await chat.bodies())[-1] in any_lang("handoff_created")  # the customer was answered
    assert [row.status for row in await chat.handoffs()] == ["open"]  # the handoff is still there

    channel.error = None  # the channel is back: the next pass tells staff
    retry = worker.TurnOutcome(new_handoff=True)
    await worker.announce(sessionmaker_of(chat.session), retry)

    assert len(channel.sent) == 1


async def test_a_budget_alert_reaches_the_owner_and_holds_no_customer_data(
    chat: Chat, channel: Channel
) -> None:
    chat.use_settings(daily_cost_budget=1.0, cost_per_text_message=1.0)

    _, outcome = await chat.say("my number is 0300 1234567")

    assert len(outcome.alerts) == 1
    await worker.announce(sessionmaker_of(chat.session), outcome)
    owner_messages = [n for n in channel.sent if n.subject.startswith("[Bot]")]
    assert len(owner_messages) == 1
    assert "0300" not in owner_messages[0].body and "1234567" not in owner_messages[0].body
