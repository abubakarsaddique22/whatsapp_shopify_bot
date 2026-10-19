"""Ask the REAL bot one customer message (or a short conversation) and read what it did.

Shared by scripts/run_eval.py (the runner) and tests/llm (the `-m llm` evaluation tests), so the
two cannot drift apart. Nothing is sent to WhatsApp. Every call starts a NEW conversation (state
reset, no checkpointer), so one answer never helps the next one.

    deps_args = build_deps_args(ollama=True)          # ConfigError if no real model is set up
    with watching_provider_errors() as catcher:
        run = await ask_with_retry(deps_args, ["GA-2100 ka price?"], catcher)
    run.reply, run.router_language, run.intent, run.tools, run.errors

Needs Postgres (docker compose up -d), the real dev store and LLM_* in .env.
"""

import asyncio
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.agent.graph import build_graph, run_turn, to_replies
from app.agent.nodes import Deps
from app.core.errors import ConfigError
from app.core.settings import get_settings
from app.core.tracing import configure_tracing
from app.db import repositories as repo
from app.db.session import get_sessionmaker
from app.domain.models import ConversationState, Lang
from app.integrations.llm.chat_factory import get_chat_model, get_eval_chat_model
from app.integrations.llm.fake_llm import FakeLLM

EVAL_WA_ID = "local-eval-run"  # the one fake customer of the eval runs (a row in conversations)
EVAL_HASH = "eval-run"
PROVIDER_TRIES = 3  # a question is asked up to 3 times when the provider fails (429, timeout)
PROVIDER_WAIT = 20.0  # seconds before the first retry, doubled each time


@dataclass
class BotRun:
    """What one question did."""

    reply: str  # the text replies joined; "[ERROR ...]" or "[PROVIDER FAILED ...]" when it failed
    router_language: Lang | None = None  # the language the router named
    intent: str = ""  # the intent the router gave
    tools: list[str] = field(default_factory=list)  # names of the tools that ran, in order
    errors: list[str] = field(default_factory=list)  # tool error codes
    provider_failures: int = 0

    @property
    def failed(self) -> bool:
        """The run itself failed (provider down, crash): not a result about the bot."""
        return self.reply.startswith(("[PROVIDER FAILED", "[ERROR"))


def build_deps_args(*, ollama: bool = False) -> dict[str, Any]:
    """The models and settings for `ask_bot`. Ollama (the eval model) or the app's Groq models."""
    settings = get_settings()
    configure_tracing(settings)
    if ollama:
        llm = composer_llm = get_eval_chat_model()
    else:
        llm = get_chat_model("router")
        composer_llm = get_chat_model("composer")
    if isinstance(llm, FakeLLM):
        raise ConfigError("the evaluation needs a real model: set LLM_PROVIDER=groq in .env")
    return {
        "llm": llm,
        "composer_llm": composer_llm,
        "urdu_script_replies": settings.urdu_script_replies,
    }


class ProviderErrorCatcher(logging.Handler):
    """Notices 'router_provider_error' / 'composer_provider_error' log lines.

    The router and the composer swallow a provider failure and the bot answers with a polite
    fallback ('I did not understand'). To the scorer that looks like a wrong answer, but it is the
    provider's fault (rate limit), not the bot's facts. So the question is asked again.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.hits = 0

    def emit(self, record: logging.LogRecord) -> None:
        if "provider_error" in record.getMessage():
            self.hits += 1


@contextmanager
def watching_provider_errors() -> Iterator[ProviderErrorCatcher]:
    catcher = ProviderErrorCatcher()
    logging.getLogger().addHandler(catcher)
    try:
        yield catcher
    finally:
        logging.getLogger().removeHandler(catcher)


async def ask_bot(deps_args: dict[str, Any], texts: list[str]) -> BotRun:
    """The customer's messages, one after the other, in ONE fresh conversation -> the last reply.

    One message is the normal case. A follow-up question ('iska price?') sends the message that
    names the product first; the conversation state is carried from turn to turn like in the app.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        conversation_id = await repo.get_or_create_conversation(session, EVAL_WA_ID, EVAL_HASH)
        conversation = ConversationState.model_validate(
            {"conversation_id": str(conversation_id), "sender_hash": EVAL_HASH}
        )
        await repo.save_state(session, conversation_id, conversation)
        deps = Deps(session=session, **deps_args)
        graph = build_graph(deps, None)
        for text in texts:
            state = await run_turn(
                graph,
                conversation_id,
                text,
                conversation,
                correlation_id=uuid.uuid4().hex,
            )
            conversation = state["conversation"]
        replies = to_replies(state)
    routed = conversation.last_router_output
    return BotRun(
        reply="\n".join(r.body for r in replies if r.kind != "image" and r.body),
        router_language=routed.language if routed else None,
        intent=routed.intent.value if routed else "",
        tools=[result.tool for result in state.get("tool_results", [])],
        errors=[str(e) for e in state.get("tool_errors", [])],
    )


async def ask_with_retry(
    deps_args: dict[str, Any], texts: list[str], catcher: ProviderErrorCatcher
) -> BotRun:
    """Retries only when the provider failed, never on a bad answer."""
    wait = PROVIDER_WAIT
    failures = 0
    run = BotRun(reply="")
    for attempt in range(1, PROVIDER_TRIES + 1):
        catcher.hits = 0
        try:
            run = await ask_bot(deps_args, texts)
        except Exception as exc:  # a crash is a wrong answer, not the end of the run
            run = BotRun(reply=f"[ERROR {type(exc).__name__}] {exc}")
        if not catcher.hits:
            run.provider_failures = failures
            return run
        failures += 1
        if attempt < PROVIDER_TRIES:
            print(f"      provider error, asking again in {wait:.0f}s ({attempt}/{PROVIDER_TRIES})")
            await asyncio.sleep(wait)
            wait *= 2
    run.reply = f"[PROVIDER FAILED {failures}x] {run.reply}"
    run.provider_failures = failures
    return run
