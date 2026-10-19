"""The kill switch (Blueprint 9.6): one switch stops the bot, and an attacker cannot undo it.

Two ways to switch it on: KILL_SWITCH=true in .env (cannot be undone from the API) and the
`runtime_flags` row the admin API flips (the worker is another process, so it reads the row).
The worker checks it first for every message and again before every send.

Needs: docker compose up -d db, and uv run alembic upgrade head.
"""

import httpx
import pytest

from app import worker
from app.api import admin
from app.core.settings import Settings
from app.db import repositories as repo
from app.handoff import service as handoff_service
from tests.adversarial.conftest import ADMIN_HEADERS, ADMIN_KEY
from tests.adversarial.harness import router_says
from tests.adversarial.worker_kit import ATTACK, Chat, any_lang, sessionmaker_of

SWITCHES = [
    pytest.param("setting", id="KILL_SWITCH-setting"),
    pytest.param("flag", id="database-flag"),
]


async def switch_on(chat: Chat, how: str) -> None:
    if how == "setting":
        chat.use_settings(kill_switch=True)
    else:
        await repo.set_flag(chat.session, repo.KILL_SWITCH_FLAG, True)


@pytest.mark.parametrize("how", SWITCHES)
async def test_a_stopped_bot_is_silent_and_makes_no_model_call(chat: Chat, how: str) -> None:
    await switch_on(chat, how)
    chat.use_model(router_says("greeting"))

    inbox_id, outcome = await chat.say("hello")

    assert await chat.status(inbox_id) == "ignored"
    assert await chat.bodies() == []  # no reply, not even a notice
    assert chat.llm.calls == []  # no model call, no cost
    assert await chat.handoffs() == []
    assert outcome.alerts == [] and outcome.new_handoff is False


@pytest.mark.parametrize("how", SWITCHES)
async def test_the_kill_switch_comes_before_every_other_rule(chat: Chat, how: str) -> None:
    await handoff_service.pause(chat.session, await chat.conversation_id(), hours=1)
    await switch_on(chat, how)

    inbox_id, _ = await chat.say("anyone there?")

    assert await chat.status(inbox_id) == "ignored"
    assert await chat.bodies() == []  # a paused customer does not even get the pause notice
    assert await chat.kept_messages() == []  # nothing was stored


async def test_turning_the_switch_off_restarts_the_bot_but_old_messages_stay_unanswered(
    chat: Chat,
) -> None:
    await repo.set_flag(chat.session, repo.KILL_SWITCH_FLAG, True)
    during, _ = await chat.say("hello")

    await repo.set_flag(chat.session, repo.KILL_SWITCH_FLAG, False)
    after, _ = await chat.say("hello again")

    assert await chat.status(during) == "ignored"  # never answered, not replayed later
    assert await chat.status(after) == "done"
    assert len(await chat.bodies()) == 1  # only the answer to the new message


@pytest.mark.parametrize("how", SWITCHES)
async def test_a_reply_that_was_already_queued_is_not_sent_while_stopped(
    chat: Chat, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    sent: list[str] = []

    async def fake_send_one(session: object) -> bool:
        sent.append("sent")
        return True

    monkeypatch.setattr(worker, "send_one", fake_send_one)
    await chat.say("hmm")  # a reply is now waiting in the outbox
    assert len(await chat.bodies()) == 1

    await switch_on(chat, how)
    assert await worker.send_outbox(sessionmaker_of(chat.session)) is False
    assert sent == []  # nothing left the building

    if how == "flag":  # the flag can be turned off again; the .env setting needs a restart
        await repo.set_flag(chat.session, repo.KILL_SWITCH_FLAG, False)
        assert await worker.send_outbox(sessionmaker_of(chat.session)) is True
        assert sent == ["sent"]


# ---- the admin API: an attacker cannot undo the switch


async def test_a_wrong_key_cannot_turn_the_kill_switch_off(
    admin_client: httpx.AsyncClient, chat: Chat
) -> None:
    await admin_client.post("/admin/kill-switch", json={"on": True}, headers=ADMIN_HEADERS)
    assert await repo.get_flag(chat.session, repo.KILL_SWITCH_FLAG) is True

    for headers in ({}, {"X-Admin-Key": "x" * 32}, {"X-Admin-Key": ADMIN_KEY[:-1]}):
        attempt = await admin_client.post("/admin/kill-switch", json={"on": False}, headers=headers)
        assert attempt.status_code == 401

    assert await repo.get_flag(chat.session, repo.KILL_SWITCH_FLAG) is True  # still stopped
    inbox_id, _ = await chat.say("hello")
    assert await chat.status(inbox_id) == "ignored"


async def test_the_env_setting_cannot_be_undone_through_the_api(
    admin_client: httpx.AsyncClient, chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = Settings(
        _env_file=None, admin_endpoints_enabled=True, admin_api_key=ADMIN_KEY, kill_switch=True
    )
    monkeypatch.setattr(admin, "get_settings", lambda: stopped)
    chat.use_settings(kill_switch=True)

    answer = await admin_client.post(
        "/admin/kill-switch", json={"on": False}, headers=ADMIN_HEADERS
    )

    assert answer.status_code == 200
    assert answer.json() == {"flag": False, "setting": True, "active": True}
    inbox_id, _ = await chat.say("hello")
    assert await chat.status(inbox_id) == "ignored"  # the bot stays stopped


async def test_a_stopped_bot_ignores_an_attack_too(chat: Chat) -> None:
    await repo.set_flag(chat.session, repo.KILL_SWITCH_FLAG, True)
    chat.use_model(router_says("abuse_or_injection"))

    inbox_id, _ = await chat.say(ATTACK)

    assert await chat.status(inbox_id) == "ignored"
    assert chat.llm.calls == []
    assert await chat.events("injection_suspected") == 0  # nothing ran, so no strike either


async def test_the_bot_answers_normally_when_the_switch_was_never_used(chat: Chat) -> None:
    inbox_id, _ = await chat.say("hmm")

    assert await chat.status(inbox_id) == "done"
    assert (await chat.bodies())[0] in any_lang("ask_detail")
