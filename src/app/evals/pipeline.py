"""Drives the REAL worker on the REAL database for the duplicate and latency evaluators.

    scripts/run_eval.py --set duplicates      (T16 and the chaos scenarios, no real model)
    scripts/run_eval.py --set latency         (webhook to outbox, the app's real models)

Unlike the integration tests (one transaction, rolled back) these runs COMMIT, because the things
to prove need it: two workers racing, a worker that dies before its commit, a webhook that is
delivered twice at the same moment. So the rules are strict:

* the worker must be stopped and the queue empty (`ensure_ready`), or the run refuses to start;
* every customer is fake (wa_id starts with EVAL_PREFIX); their rows are deleted at the end of
  every scenario (ON DELETE CASCADE from `conversations`) and at the start of the next run, in
  case an earlier run died;
* nothing is sent to WhatsApp: `sender.send_text` is replaced by `FakeMeta`, which counts what
  Meta WOULD have accepted;
* no mail, no Slack: the notifier is not called (`announce` is not part of these runs).

Scoring lives in duplicates.py and latency.py (pure, unit tested). This file only drives.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import SecretStr
from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import worker
from app.api import whatsapp_webhook
from app.core.errors import ConfigError, WhatsAppTemporaryError
from app.core.security import sign_whatsapp
from app.core.settings import Settings
from app.db import repositories as repo
from app.db.tables import Conversation, WhatsAppInbox, WhatsAppOutbox
from app.evals.duplicates import ScenarioResult, Tally
from app.evals.latency import LatencyRun
from app.evals.runner import ProviderErrorCatcher
from app.integrations.llm.fake_llm import FakeLLM
from app.integrations.whatsapp import sender
from app.integrations.whatsapp.payloads import InboundMessage
from app.main import app as fastapi_app

EVAL_PREFIX = "eval-pipe-"  # every fake customer's wa_id starts with this
WEBHOOK_SECRET = "eval-app-secret"  # the fake app secret the evaluation signs its webhooks with
DRAIN_ROUNDS = 40  # how often the workers are started again while something is still pending
WAITING = ("pending", "processing")


# ---- small helpers


@contextmanager
def patched(target: object, **attrs: Any) -> Iterator[None]:
    """Replace attributes of a module for the length of the block, then put them back."""
    old = {name: getattr(target, name) for name in attrs}
    for name, value in attrs.items():
        setattr(target, name, value)
    try:
        yield
    finally:
        for name, value in old.items():
            setattr(target, name, value)


@contextmanager
def quiet_logs() -> Iterator[None]:
    """A simulated crash makes the worker log a traceback on purpose: hide those lines."""
    logging.disable(logging.ERROR)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


def new_customers(count: int) -> list[str]:
    run = uuid.uuid4().hex[:8]
    return [f"{EVAL_PREFIX}{run}-{i:02d}" for i in range(count)]


def new_wamid() -> str:
    return f"wamid.eval-{uuid.uuid4().hex[:12]}"


def new_hash() -> str:
    return f"ev{uuid.uuid4().hex[:12]}"  # 14 characters: wa_id_hash is 16 at most


def eval_settings() -> Settings:
    """The worker's settings for the chaos scenarios: no .env, nothing that could change the
    number of replies (no debug reply, team always open, limits far away)."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        debug_reply=False,
        business_hours="00:00-00:00",
        rate_limit_per_minute=100,
        rate_limit_per_hour=1000,
        rate_limit_per_day=10000,
    )


def fake_model(role: str = "router") -> FakeLLM:
    """An empty FakeLLM: the router answers 'unclear', the bot asks for detail. One reply."""
    return FakeLLM([])


async def _count(session: AsyncSession, stmt: Select[Any]) -> int:
    return int((await session.execute(stmt)).scalar_one())


def _count_of(table: Any, *conditions: Any) -> Select[Any]:
    return select(func.count()).select_from(table).where(*conditions)


def _eval_rows(table: Any, *statuses: str) -> Select[Any]:
    """Count of rows of the eval customers in `table` (inbox or outbox), optionally by status."""
    stmt = (
        select(func.count())
        .select_from(table)
        .join(Conversation, Conversation.id == table.conversation_id)
        .where(Conversation.wa_id.like(f"{EVAL_PREFIX}%"))
    )
    return stmt.where(table.status.in_(statuses)) if statuses else stmt


async def remove_eval_rows(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Delete every fake customer: inbox, outbox, messages and handoffs go with them."""
    async with sessionmaker() as session:
        await session.execute(
            delete(Conversation).where(Conversation.wa_id.like(f"{EVAL_PREFIX}%"))
        )
        await session.commit()


async def ensure_ready(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Refuse to run next to a live worker or on top of someone's queue."""
    await remove_eval_rows(sessionmaker)  # leftovers of an earlier run that died
    async with sessionmaker() as session:
        waiting_in = await _count(
            session, _count_of(WhatsAppInbox, WhatsAppInbox.status.in_(WAITING))
        )
        waiting_out = await _count(
            session, _count_of(WhatsAppOutbox, WhatsAppOutbox.status == "pending")
        )
        stopped = await repo.get_flag(session, repo.KILL_SWITCH_FLAG)
    if waiting_in or waiting_out:
        raise ConfigError(
            f"{waiting_in} inbound and {waiting_out} outbound messages are waiting in the queue. "
            "Stop the worker (python -m app.worker) and let the queue empty, then run again: "
            "this evaluation runs the real worker functions and must be the only one."
        )
    if stopped:
        raise ConfigError("the kill switch is ON in the database: switch it off first")


# ---- the fake Meta


class FakeMeta:
    """Stands in for Meta's send API and counts every message it ACCEPTS, per recipient.

    temporary_error_first: the first call to each recipient fails like a Meta 5xx (nothing is
        accepted), the retry works.
    crash_after_accept_first: the first call to each recipient is accepted and THEN the worker
        dies (an exception), so the row is never marked sent and is sent again later. This is the
        one hole that no database rule can close: Meta has no idempotency key.
    """

    def __init__(
        self, *, temporary_error_first: bool = False, crash_after_accept_first: bool = False
    ) -> None:
        self.accepted: dict[str, int] = {}
        self._seen: set[str] = set()
        self.temporary_error_first = temporary_error_first
        self.crash_after_accept_first = crash_after_accept_first

    async def send_text(self, to: str, body: str) -> str:
        first = to not in self._seen
        self._seen.add(to)
        if first and self.temporary_error_first:
            raise WhatsAppTemporaryError("HTTP 503", detail={"meta_code": None})
        self.accepted[to] = self.accepted.get(to, 0) + 1
        if first and self.crash_after_accept_first:
            raise RuntimeError("the worker died after Meta accepted the message")
        return f"wamid.eval-out-{uuid.uuid4().hex[:12]}"


# ---- the workers


async def _anything_pending(sessionmaker: async_sessionmaker[AsyncSession]) -> bool:
    async with sessionmaker() as session:
        waiting_in = await _count(session, _eval_rows(WhatsAppInbox, *WAITING))
        waiting_out = await _count(session, _eval_rows(WhatsAppOutbox, "pending"))
    return bool(waiting_in or waiting_out)


async def drain(sessionmaker: async_sessionmaker[AsyncSession], workers: int) -> None:
    """Run `workers` real worker loops at the same time until the queue is empty.

    One loop is exactly what worker.main() does each pass: process one inbound message, send one
    reply. A loop stops when a pass finds nothing to do (or an error ended the pass), then the
    queue is looked at again: a row that is still pending starts the loops again.
    """

    async def one_worker() -> None:
        while True:
            processed = await worker.process_inbox(sessionmaker)
            sent = await worker.send_outbox(sessionmaker)
            if not (processed or sent):
                return

    for _ in range(DRAIN_ROUNDS):
        await asyncio.gather(*(one_worker() for _ in range(workers)))
        if not await _anything_pending(sessionmaker):
            return


# ---- webhooks


def webhook_body(wa_id: str, wamid: str, text: str) -> dict[str, Any]:
    message = {
        "from": wa_id,
        "id": wamid,
        "timestamp": "1760169600",
        "type": "text",
        "text": {"body": text},
    }
    value = {"messaging_product": "whatsapp", "messages": [message]}
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": value}]}]}


@asynccontextmanager
async def webhook_client() -> AsyncIterator[httpx.AsyncClient]:
    """The real FastAPI app, called in-process (no server, no ngrok), real database."""
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://eval") as client:
        yield client


async def deliver(
    client: httpx.AsyncClient, wa_id: str, wamid: str, text: str, times: int
) -> None:
    """Post the SAME signed webhook `times` times, all at once (Meta retrying while we are slow)."""
    raw = json.dumps(webhook_body(wa_id, wamid, text)).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sign_whatsapp(raw, WEBHOOK_SECRET),
    }
    answers = await asyncio.gather(
        *(client.post("/webhook/whatsapp", content=raw, headers=headers) for _ in range(times))
    )
    for answer in answers:
        if answer.status_code != 200:
            raise RuntimeError(f"the webhook answered {answer.status_code}")


# ---- duplicates: T16 and the chaos scenarios


@dataclass
class Scenario:
    name: str
    what: str
    customers: int = 3
    messages_each: int = 1  # different messages per customer
    deliveries: int = 1  # how often each message is delivered (all at the same moment)
    workers: int = 1  # worker loops at the same time
    meta: dict[str, bool] = field(default_factory=dict)  # FakeMeta options
    crash_once: bool = False  # every inbound message crashes the worker once, before the commit
    known_gap: bool = False


SCENARIOS = [
    Scenario(
        "t16_webhook_twice",
        "T16: the same webhook arrives 3 times at once: one inbox row, one reply",
        deliveries=3,
    ),
    Scenario(
        "redelivery_burst",
        "two different messages per customer, each delivered twice at once, same customer",
        messages_each=2,
        deliveries=2,
    ),
    Scenario(
        "worker_dies_before_commit",
        "the worker finishes the work and dies before its commit: the retry must not add a reply",
        crash_once=True,
    ),
    Scenario(
        "two_workers_race",
        "three workers take from the same queue, every webhook delivered twice",
        customers=6,
        deliveries=2,
        workers=3,
    ),
    Scenario(
        "meta_temporary_error",
        "Meta answers 5xx once, the sender retries: one accepted message per reply",
        meta={"temporary_error_first": True},
    ),
    Scenario(
        "meta_accepts_then_crash",
        "the worker dies AFTER Meta accepted the message and before the row is marked sent: "
        "the reply is sent again. Meta has no idempotency key, so no database rule closes this "
        "window (blueprint 5.7 marks the row sent only after Meta accepts). Decide with the "
        "client; a gate that counts this one cannot be met",
        customers=2,
        meta={"crash_after_accept_first": True},
        known_gap=True,
    ),
]


async def _tallies(
    sessionmaker: async_sessionmaker[AsyncSession],
    scenario: Scenario,
    wa_ids: Sequence[str],
    meta: FakeMeta,
) -> dict[str, Tally]:
    out: dict[str, Tally] = {}
    async with sessionmaker() as session:
        for wa_id in wa_ids:
            conversation_id = (
                await session.execute(select(Conversation.id).where(Conversation.wa_id == wa_id))
            ).scalar_one_or_none()
            inbox = queued = 0
            if conversation_id is not None:
                inbox = await _count(
                    session,
                    _count_of(WhatsAppInbox, WhatsAppInbox.conversation_id == conversation_id),
                )
                queued = await _count(
                    session,
                    _count_of(WhatsAppOutbox, WhatsAppOutbox.conversation_id == conversation_id),
                )
            out[wa_id] = Tally(
                wamids=scenario.messages_each,
                inbox=inbox,
                queued=queued,
                sent=meta.accepted.get(wa_id, 0),
            )
    return out


async def run_scenario(
    sessionmaker: async_sessionmaker[AsyncSession], client: httpx.AsyncClient, scenario: Scenario
) -> ScenarioResult:
    wa_ids = new_customers(scenario.customers)
    meta = FakeMeta(**scenario.meta)
    crashed: set[int] = set()
    real_handle_inbox = worker.handle_inbox
    settings = eval_settings()
    webhook_settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        whatsapp_app_secret=SecretStr(WEBHOOK_SECRET),
        whatsapp_verify_token=SecretStr("eval-verify"),
        phone_hash_pepper=SecretStr("eval-pepper"),
    )

    async def handle_inbox(session: AsyncSession, inbox: WhatsAppInbox) -> worker.TurnOutcome:
        outcome = await real_handle_inbox(session, inbox)
        if scenario.crash_once and inbox.id not in crashed:
            crashed.add(inbox.id)
            raise RuntimeError("the worker died before the commit")
        return outcome

    result = ScenarioResult(scenario.name, scenario.what, scenario.deliveries)
    result.known_gap = scenario.known_gap
    try:
        with (
            quiet_logs(),
            patched(
                worker,
                get_settings=lambda: settings,
                get_chat_model=fake_model,
                handle_inbox=handle_inbox,
            ),
            patched(whatsapp_webhook, get_settings=lambda: webhook_settings),
            patched(sender, send_text=meta.send_text),
        ):
            for wa_id in wa_ids:
                for number in range(scenario.messages_each):
                    await deliver(
                        client, wa_id, new_wamid(), f"hmm {number + 1}", scenario.deliveries
                    )
            await drain(sessionmaker, scenario.workers)
        result.tallies = await _tallies(sessionmaker, scenario, wa_ids, meta)
    except Exception as exc:  # the scenario broke: show it, do not hide it
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        await remove_eval_rows(sessionmaker)
    return result


async def run_duplicate_scenarios(
    sessionmaker: async_sessionmaker[AsyncSession],
    on_result: Callable[[ScenarioResult], None] | None = None,
) -> list[ScenarioResult]:
    """T16 and the chaos scenarios, one after the other. ConfigError if the queue is not empty."""
    await ensure_ready(sessionmaker)
    results = []
    async with webhook_client() as client:
        for scenario in SCENARIOS:
            result = await run_scenario(sessionmaker, client, scenario)
            results.append(result)
            if on_result is not None:
                on_result(result)
    return results


# ---- latency: webhook to outbox, the app's real models


def ensure_real_settings() -> None:
    """The latency numbers are only worth something with the app's real settings."""
    settings = worker.get_settings()
    if settings.debug_reply:
        raise ConfigError("DEBUG_REPLY=true skips the graph: set it to false for a latency run")
    if settings.kill_switch:
        raise ConfigError("KILL_SWITCH=true in .env: the worker would ignore every message")


class ProviderTroubleCatcher(ProviderErrorCatcher):
    """Also notices the OpenAI client's own 'Retrying request' lines: a 429 that was waited out
    and then worked. The answer is right but the time holds the wait, so it is not a latency."""

    def __init__(self) -> None:
        super().__init__()
        self.setLevel(logging.INFO)
        self.retries = 0

    def emit(self, record: logging.LogRecord) -> None:
        if "Retrying request" in record.getMessage():
            self.retries += 1
        else:
            super().emit(record)


async def _time_one(
    sessionmaker: async_sessionmaker[AsyncSession],
    question_id: str,
    text: str,
    catcher: ProviderTroubleCatcher,
) -> LatencyRun:
    """One fresh customer, one question: the time from 'stored in the inbox' to 'reply rows
    committed' through the real worker code."""
    wa_id = new_customers(1)[0]
    wa_hash = new_hash()
    message = InboundMessage(wamid=new_wamid(), wa_id=wa_id, msg_type="text", text=text)
    async with sessionmaker() as session:
        conversation_id = await repo.get_or_create_conversation(session, wa_id, wa_hash)
        inbox_id = await repo.add_inbox(session, conversation_id, wa_hash, message)
        await session.commit()
    if inbox_id is None:
        return LatencyRun(question_id, "text", 0.0, ok=False, note="inbox row was not stored")

    catcher.hits = 0
    catcher.retries = 0
    started = time.perf_counter()
    try:
        async with sessionmaker() as session:
            inbox = await session.get(WhatsAppInbox, inbox_id)
            if inbox is None:
                raise LookupError("inbox row vanished")
            await worker.handle_inbox(session, inbox)
            await session.commit()
    except Exception as exc:
        seconds = time.perf_counter() - started
        return LatencyRun(question_id, "text", seconds, ok=False, note=f"crash: {exc!r}"[:120])
    seconds = time.perf_counter() - started

    async with sessionmaker() as session:
        status = (
            await session.execute(select(WhatsAppInbox.status).where(WhatsAppInbox.id == inbox_id))
        ).scalar_one()
        replies = await _count(
            session, _count_of(WhatsAppOutbox, WhatsAppOutbox.inbox_id == inbox_id)
        )
    if catcher.hits:
        return LatencyRun(
            question_id, "text", seconds, ok=False, note="provider error: the bot fell back"
        )
    if catcher.retries:
        return LatencyRun(
            question_id,
            "text",
            seconds,
            ok=False,
            note=f"rate limit (429): {catcher.retries} retry wait(s) are in the time",
        )
    if status != "done" or not replies:
        return LatencyRun(
            question_id, "text", seconds, ok=False, note=f"inbox {status}, {replies} replies"
        )
    return LatencyRun(question_id, "text", seconds)


async def measure_text(
    sessionmaker: async_sessionmaker[AsyncSession],
    questions: Sequence[tuple[str, str]],
    *,
    delay: float,
    on_run: Callable[[int, int, LatencyRun], None] | None = None,
) -> list[LatencyRun]:
    """Time every (id, text) question through the real worker with the app's real models."""
    ensure_real_settings()
    await ensure_ready(sessionmaker)
    runs: list[LatencyRun] = []
    catcher = ProviderTroubleCatcher()
    logging.getLogger().addHandler(catcher)
    try:
        for number, (question_id, text) in enumerate(questions, start=1):
            run = await _time_one(sessionmaker, question_id, text, catcher)
            runs.append(run)
            if on_run is not None:
                on_run(number, len(questions), run)
            await asyncio.sleep(delay)
    finally:
        logging.getLogger().removeHandler(catcher)
        await remove_eval_rows(sessionmaker)
    return runs
