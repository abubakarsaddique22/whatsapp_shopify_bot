r"""Phase 12, Step 3: ask the REAL bot every question of an evaluation set and score the answers.

    set PYTHONPATH=src
    uv run python scripts\run_eval.py
    uv run python scripts\run_eval.py --kind price --limit 5
    uv run python scripts\run_eval.py --delay 2

Needs LLM_* and COMPOSER_MODEL in .env, Postgres running (docker compose up -d), the real dev
store. Nothing is sent to WhatsApp. Every question starts a NEW conversation (state reset, no
checkpointer), so one answer never helps the next one. Exit code 1 if any answer is wrong.
"""

import argparse
import asyncio
import io
import logging
import sys
import uuid
from typing import Any

from app.agent.graph import build_graph, run_turn, to_replies, use_selector_event_loop
from app.agent.nodes import Deps
from app.core.settings import get_settings
from app.core.tracing import configure_tracing
from app.db import repositories as repo
from app.db.session import get_engine, get_sessionmaker
from app.domain.models import ConversationState
from app.evals import product_qa
from app.integrations.llm.chat_factory import get_chat_model, get_eval_chat_model
from app.integrations.llm.fake_llm import FakeLLM

EVAL_WA_ID = "local-eval-run"  # the one fake customer of this script (a row in conversations)
EVAL_HASH = "eval-run"
PROVIDER_TRIES = 3  # a question is asked up to 3 times when the provider fails (429, timeout)
PROVIDER_WAIT = 20.0  # seconds before the first retry, doubled each time
LAST_ERRORS: list[str] = []  # tool error codes of the last question (shown next to a WRONG)


class ProviderErrorCatcher(logging.Handler):
    """Notices 'router_provider_error' / 'composer_provider_error' log lines.

    The router and the composer swallow a provider failure and the bot answers with a polite
    fallback ('I did not understand'). To the scorer that looks like a wrong answer, but it is the
    provider's fault (rate limit), not the bot's facts. So the runner asks the question again.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.hits = 0

    def emit(self, record: logging.LogRecord) -> None:
        if "provider_error" in record.getMessage():
            self.hits += 1


async def ask_with_retry(
    deps_args: dict[str, Any], texts: list[str], catcher: ProviderErrorCatcher
) -> tuple[str, int]:
    """(reply, provider_failures). Retries only when the provider failed, never on a bad answer."""
    wait = PROVIDER_WAIT
    failures = 0
    reply = ""
    for attempt in range(1, PROVIDER_TRIES + 1):
        catcher.hits = 0
        try:
            reply = await ask_bot(deps_args, texts)
        except Exception as exc:  # a crash is a wrong answer, not the end of the run
            reply = f"[ERROR {type(exc).__name__}] {exc}"
        if not catcher.hits:
            return reply, failures
        failures += 1
        if attempt < PROVIDER_TRIES:
            print(f"      provider error, asking again in {wait:.0f}s ({attempt}/{PROVIDER_TRIES})")
            await asyncio.sleep(wait)
            wait *= 2
    return f"[PROVIDER FAILED {failures}x] {reply}", failures


async def ask_bot(deps_args: dict[str, Any], texts: list[str]) -> str:
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
    LAST_ERRORS[:] = [str(e) for e in state.get("tool_errors", [])]
    return "\n".join(r.body for r in replies if r.kind != "image" and r.body)


async def run(args: argparse.Namespace) -> int:
    natural = args.set == "natural"
    rows = product_qa.load_rows(product_qa.NATURAL if natural else product_qa.DATA)
    vocab = product_qa.vocabulary(rows)
    chosen = product_qa.select(rows, args.kind, args.limit)
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        chosen = [r for r in rows if r["id"] in wanted]
    settings = get_settings()
    configure_tracing(settings)
    if args.ollama:
        llm = composer_llm = get_eval_chat_model()
    else:
        llm = get_chat_model("router")
        composer_llm = get_chat_model("composer")
    if isinstance(llm, FakeLLM):
        raise SystemExit("run_eval needs a real model: set LLM_PROVIDER=groq in .env")
    deps_args = {
        "llm": llm,
        "composer_llm": composer_llm,
        "urdu_script_replies": settings.urdu_script_replies,
    }
    label = "Natural" if natural else "Product"
    print(f"{label} Q&A: {len(chosen)} questions, real model, real store\n")
    catcher = ProviderErrorCatcher()
    logging.getLogger().addHandler(catcher)
    results = []
    try:
        for number, row in enumerate(chosen, start=1):
            texts = [*row.get("setup", []), row["question"]]
            reply, _ = await ask_with_retry(deps_args, texts, catcher)
            if natural:
                result = product_qa.score_natural(row, reply)
                mark = {"correct": "ok", "clarified": "asked"}.get(result.verdict, "WRONG")
            else:
                result = product_qa.score(row, reply, vocab)
                mark = "ok" if result.ok else "WRONG"
            results.append(result)
            print(f"[{number}/{len(chosen)}] {row['id']} {mark}")
            if not result.ok and LAST_ERRORS:
                print(f"      tool errors: {', '.join(LAST_ERRORS)}")
            await asyncio.sleep(args.delay)
    finally:
        await get_engine().dispose()
    print()
    report = product_qa.format_natural_report if natural else product_qa.format_report
    print(report(results))
    return 0 if all(r.ok for r in results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the evaluation sets against the real bot")
    parser.add_argument("--kind", choices=["price", "stock", "colors", "sizes"])
    parser.add_argument(
        "--set",
        choices=["golden", "natural"],
        default="golden",
        help="golden: exact titles, every answer must be right. natural: messy questions, "
        "no wrong fact allowed (asking is fine)",
    )
    parser.add_argument("--limit", type=int, help="only the first N questions")
    parser.add_argument("--ids", help="only these question ids, e.g. price-31,price-32")
    parser.add_argument(
        "--ollama", action="store_true", help="router and composer through Ollama, not Groq"
    )
    parser.add_argument(
        "--delay", type=float, default=3.0, help="seconds between questions (Groq rate limit)"
    )
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):  # Urdu text must not crash a Windows console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    use_selector_event_loop()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
