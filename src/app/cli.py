"""Command line helpers. Run from the project folder:

uv run python -m app.cli shopify-ping
uv run python -m app.cli import-catalog
uv run python -m app.cli ask "GA-2100 ka price?"
uv run python -m app.cli ask "is it water resistant" --debug
uv run python -m app.cli chat
uv run python -m app.cli chat --debug
uv run python -m app.cli chat --no-checkpoint
uv run python -m app.cli eval-router
uv run python -m app.cli eval-router --limit 10
uv run python -m app.cli eval-router --provider groq
uv run python -m app.cli rag-ingest
uv run python -m app.cli rag-ingest --from-dir evals/rag_corpus
uv run python -m app.cli rag-search "refund kitne din mein milta hai?" --en "refund time"
uv run python -m app.cli rag-search "shipping policy" --type policy_shipping
uv run python -m app.cli rag-eval
uv run python -m app.cli rag-eval --threshold 0.25
uv run python -m app.cli rag-eval --embedder fake
"""

import argparse
import asyncio
import io
import json
import sys
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import rules
from app.agent.graph import (
    build_graph,
    open_checkpointer,
    run_turn,
    to_replies,
    use_selector_event_loop,
)
from app.agent.nodes import Deps
from app.agent.router_eval import format_report, load_golden, run_eval, summarize
from app.agent.state import GraphState
from app.catalog.importer import sync_mirror
from app.core.errors import AppError, ConfigError
from app.core.settings import get_settings
from app.core.tracing import configure_tracing
from app.db import repositories as repo
from app.db.session import get_engine, get_sessionmaker
from app.db.tables import KbDocument
from app.domain.models import ConversationState
from app.integrations.embeddings.base import Embedder
from app.integrations.embeddings.factory import get_embedder, get_eval_embedder, warm_up
from app.integrations.llm.chat_factory import get_chat_model, get_eval_chat_model
from app.integrations.llm.fake_llm import FakeLLM
from app.integrations.shopify import gateway
from app.integrations.shopify.auth import shop_host
from app.integrations.shopify.client import graphql
from app.rag import rag_eval as scoring
from app.rag.ingest import (
    MANAGED_PREFIXES,
    fetch_sources,
    ingest_documents,
    load_sources_from_dir,
)
from app.rag.retrieve import retrieve

PING_QUERY = "{ shop { name currencyCode } productsCount { count } }"


async def shopify_ping() -> None:
    """Smallest possible real call: proves the token, the scopes and the store connection work."""
    data = await graphql(PING_QUERY)  # token + scope audit happen inside
    print(f"Shop:     {data['shop']['name']} ({shop_host()})")
    print(f"Currency: {data['shop']['currencyCode']}")
    print(f"Products: {data['productsCount']['count']}")
    print("Scope audit: OK (read-only scopes only)")


async def import_catalog() -> None:
    """Copy the visible products of the real store into the Postgres mirror (re-runnable)."""
    products = await gateway.list_products()  # hidden products never come back from the gateway
    try:
        async with get_sessionmaker()() as session, session.begin():  # commit, or roll back all
            report = await sync_mirror(session, products)
    finally:
        await get_engine().dispose()
    print(f"Visible products in Shopify: {report.total}")
    print(f"  added:     {report.added}")
    print(f"  updated:   {report.updated}")
    print(f"  unchanged: {report.unchanged}")
    print(f"  hidden:    {report.hidden}")
    if report.skipped_hiding:
        print("WARNING: Shopify returned 0 products, so nothing was hidden in the mirror.")


async def ask(question: str, *, debug: bool = False) -> None:
    """Answer one customer question with the rule-based reader and the live tools (no LLM).

    Reads the mirror and calls Shopify for price and stock. `--debug` also prints every tool
    result, so the numbers can be compared with the Shopify admin.
    """
    try:
        async with get_sessionmaker()() as session:
            reply = await rules.answer(session, question)
    finally:
        await get_engine().dispose()
    print(reply.text)
    if debug:
        parsed = reply.parsed
        print(
            f"\n[understood] intent={parsed.intent.value} product={parsed.product_query!r} "
            f"colour={parsed.colour} size_mm={parsed.size_mm} max_price={parsed.max_price}"
        )
        for result in reply.results:
            print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))


COMMANDS = {"shopify-ping": shopify_ping, "import-catalog": import_catalog}


async def eval_router(limit: int | None = None, provider: str = "ollama") -> None:
    """Score the REAL router model on evals/router_golden.jsonl (needs LLM_* in .env).

    Prints language / intent / colour / max_price accuracy per language class and every wrong
    case. This is the number to write into PROGRESS.md under "Baselines measured".
    """
    if provider == "ollama":  # default: Ollama has no tight rate limit, Groq stays for the app
        llm = get_eval_chat_model()
        model_name = get_settings().eval_router_model
    else:
        llm = get_chat_model("router")
        model_name = get_settings().router_model
    if isinstance(llm, FakeLLM):
        raise ConfigError("eval-router needs a real model: set LLM_PROVIDER=groq in .env")
    if isinstance(sys.stdout, io.TextIOWrapper):  # Urdu text must not crash a Windows console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cases = load_golden()
    print(f"Router model: {model_name} ({provider})   golden cases: {len(cases)}\n")
    print(format_report(summarize(await run_eval(cases, llm, limit))))


def _utf8_console() -> None:
    """Urdu text must not crash a Windows console."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def rag_ingest(from_dir: str | None = None) -> None:
    """Bring the store's policies and pages (or a folder of .md files) into the knowledge base.

    Re-runnable: unchanged documents cost nothing, a changed document gets a new version and only
    its new chunks are embedded, a document that left the store is removed.
    """
    embedder = get_embedder()
    prefixes = MANAGED_PREFIXES
    if from_dir:
        docs = load_sources_from_dir(Path(from_dir))
        prefixes = ("file:",)
        origin = f"folder {from_dir}"
    else:
        docs = await fetch_sources()
        origin = "Shopify"
    try:
        async with get_sessionmaker()() as session, session.begin():  # commit, or roll back all
            report = await ingest_documents(session, embedder, docs, managed_prefixes=prefixes)
    finally:
        await get_engine().dispose()
    print(f"Embedder: {embedder.name} / {embedder.model}")
    print(f"Documents from {origin}: {report.total}")
    print(f"  added:     {report.added}")
    print(f"  updated:   {report.updated}")
    print(f"  unchanged: {report.unchanged}")
    print(f"  removed:   {report.removed}")
    print(f"  no text:   {report.skipped_empty}")
    print(f"Chunks embedded: {report.chunks_embedded}   reused: {report.chunks_reused}")
    for flagged in report.flagged:
        print(f"WARNING: {flagged.source_key} not indexed, looks like an injection:")
        print(f"         {flagged.findings}")
    if report.skipped_removal:
        print("WARNING: the store gave no document, so nothing was removed.")


async def rag_search(question: str, query_en: str | None, doc_type: str | None) -> None:
    """Search the knowledge base like the bot does. For the manual check of Phase 8.

    --en is the short English query the router would write (use it for Roman Urdu questions).
    """
    _utf8_console()
    embedder = get_embedder()
    try:
        async with get_sessionmaker()() as session:
            found = await retrieve(
                session, embedder, query_en=query_en, original=question, doc_type=doc_type
            )
    finally:
        await get_engine().dispose()
    print(
        f"Embedder: {embedder.name} / {embedder.model}   "
        f"RAG_MIN_SCORE={get_settings().rag_min_score}   type={doc_type or 'any'}"
    )
    if found.abstained:
        best = "no passage" if found.best_score is None else f"{found.best_score:.3f}"
        print(f"\nABSTAIN: nothing is relevant enough (best score: {best}).")
        print("The bot would say this is not confirmed and offer a human.")
        return
    for number, passage in enumerate(found.passages, start=1):
        print(
            f"\n[{number}] {passage.doc_type} | {passage.heading_path} | "
            f"score {passage.score:.3f} | {passage.source_id}"
        )
        print(passage.text)
        if passage.source_url:
            print(f"    link: {passage.source_url}")


def _eval_embedder(which: str) -> Embedder:
    """local (default): the sentence-transformers model. configured: EMBEDDING_PROVIDER."""
    if which == "fake":
        return get_embedder(get_settings().model_copy(update={"embedding_provider": "fake"}))
    if which == "configured":
        return get_embedder()
    return get_eval_embedder()


async def rag_eval(
    corpus: str | None, threshold: float | None, use_filter: bool, which: str = "local"
) -> None:
    """Score retrieval on evals/rag_golden.jsonl against the sample documents in evals/rag_corpus.

    The embedder is the local sentence-transformers model (EMBEDDING_MODEL, default bge-m3)
    unless --embedder says otherwise. The sample documents are loaded inside a
    transaction that is rolled back, and every real document is switched off inside it, so your
    real knowledge base is never touched or mixed in.
    """
    _utf8_console()
    embedder = _eval_embedder(which)
    docs = load_sources_from_dir(Path(corpus) if corpus else scoring.CORPUS_DIR)
    cases = scoring.load_golden()
    level = get_settings().rag_min_score if threshold is None else threshold
    engine = get_engine()
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            session = AsyncSession(bind=conn, expire_on_commit=False)
            try:
                await session.execute(update(KbDocument).values(is_active=False))
                await ingest_documents(session, embedder, docs, managed_prefixes=("file:",))
                results = await scoring.run_cases(session, embedder, cases, use_filter=use_filter)
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()
    print(f"Embedder: {embedder.name} / {embedder.model}   documents: {len(docs)}")
    if embedder.name == "fake":
        print("NOTE: the fake embedder matches words only, so these numbers test the pipeline.")
        print("      Real hit rates (especially Roman Urdu) need a real embedding model.")
    print()
    print(scoring.format_report(results, level))


CHAT_WA_ID = "local-cli-chat"  # the one fake customer of `chat` (a row in conversations)
CHAT_HASH = "cli-chat"
CHAT_HELP = "Type a customer message.  /reset = new conversation   /quit = leave"


def _chat_debug(state: GraphState) -> str:
    """What the graph did in one turn: router result, tools, validator problems."""
    router = state.get("router")
    tools = [result.tool for result in state.get("tool_results", [])]
    lines = [
        f"  intent={router.intent.value if router else None}"
        f" language={router.language.value if router else None}"
        f" reply_in={state.get('reply_language')} branch={state.get('branch')}",
        f"  tools={tools} errors={state.get('tool_errors')}",
        f"  compose_attempts={state.get('compose_attempts')}"
        f" validation_errors={state.get('validation_errors')}"
        f" used_fallback={state.get('used_fallback')}",
    ]
    if state.get("handoff_reason"):
        lines.append(f"  handoff_reason={state.get('handoff_reason')}")
    for result in state.get("tool_results", []):  # what the product search really found
        if result.tool == "search_products" and isinstance(result.data, dict):
            found = [c.get("title") for c in result.data.get("candidates", [])]
            lines.append(f"  search: kind={result.data.get('kind')} found={found}")
    if state.get("security_event"):
        lines.append(f"  security_event={state.get('security_event')}")
    return "\n".join(lines)


async def chat(*, debug: bool = False, checkpoints: bool = True) -> None:
    """Talk to the WHOLE bot in the terminal: router, gate, tools, composer, validator.

    Uses the real model (LLM_* in .env), the real Postgres and the real Shopify dev store, but no
    WhatsApp: nothing is sent. The fake customer keeps its conversation between turns, so
    'aur colours?' after a product question works. --debug shows what the graph did.
    """
    _utf8_console()
    llm = get_chat_model("router")
    composer_llm = get_chat_model("composer")
    if isinstance(llm, FakeLLM):
        raise ConfigError("chat needs a real model: set LLM_PROVIDER=groq in .env")
    settings = get_settings()
    configure_tracing(settings)  # .env -> os.environ, or LangSmith stays off
    sessionmaker = get_sessionmaker()
    try:
        async with AsyncExitStack() as stack:
            saver = await stack.enter_async_context(open_checkpointer()) if checkpoints else None
            print("(loading the embedding model, the first start takes a while...)")
            try:  # so the first policy question does not use up the time budget of its turn
                await warm_up()
            except Exception as exc:
                print(f"(embedding model not loaded: {type(exc).__name__})")
            print(CHAT_HELP)
            while True:
                try:
                    line = (await asyncio.to_thread(input, "you> ")).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line:
                    continue
                if line == "/quit":
                    break
                try:
                    async with sessionmaker() as session, session.begin():  # commit per turn
                        conversation_id = await repo.get_or_create_conversation(
                            session, CHAT_WA_ID, CHAT_HASH
                        )
                        if line == "/reset":
                            fresh = ConversationState.model_validate(
                                {"conversation_id": str(conversation_id), "sender_hash": CHAT_HASH}
                            )
                            await repo.save_state(session, conversation_id, fresh)
                            print("bot> (new conversation)")
                            continue
                        conversation = await repo.load_state(session, conversation_id)
                        deps = Deps(
                            session=session,
                            llm=llm,
                            composer_llm=composer_llm,
                            urdu_script_replies=settings.urdu_script_replies,
                        )
                        state = await run_turn(
                            build_graph(deps, saver),
                            conversation_id,
                            line,
                            conversation,
                            correlation_id=uuid.uuid4().hex,
                        )
                        replies = to_replies(state)
                except AppError as exc:
                    print(f"bot> ERROR ({exc.code}): {exc.message}")
                    continue
                for reply in replies:
                    if reply.kind == "image":
                        print(f"bot> [picture] {reply.media_url}  ({reply.caption})")
                    else:
                        print(f"bot> {reply.body}")
                if debug:
                    print(_chat_debug(state))
    finally:
        await get_engine().dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    parser.add_argument(
        "command",
        choices=[
            *sorted(COMMANDS),
            "ask",
            "chat",
            "eval-router",
            "rag-ingest",
            "rag-search",
            "rag-eval",
        ],
    )
    parser.add_argument("question", nargs="*", help="for 'ask': the customer's sentence")
    parser.add_argument(
        "--debug", action="store_true", help="for 'ask': print the tool results; 'chat': the graph"
    )
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="for 'chat': do not use the LangGraph Postgres checkpointer",
    )
    parser.add_argument("--limit", type=int, help="for 'eval-router': only the first N cases")
    parser.add_argument(
        "--provider",
        choices=["ollama", "groq"],
        default="ollama",
        help="for 'eval-router': which model to score (default ollama)",
    )
    parser.add_argument("--from-dir", help="for 'rag-ingest': a folder of .md files, not Shopify")
    parser.add_argument("--en", help="for 'rag-search': the short English query (Roman Urdu)")
    parser.add_argument("--type", dest="doc_type", help="for 'rag-search': policy_refund, faq ...")
    parser.add_argument("--threshold", type=float, help="for 'rag-eval': score to apply")
    parser.add_argument("--corpus", help="for 'rag-eval': folder with the sample .md documents")
    parser.add_argument(
        "--no-filter", action="store_true", help="for 'rag-eval': do not filter by doc_type"
    )
    parser.add_argument(
        "--embedder",
        choices=["local", "fake", "configured"],
        default="local",
        help="for 'rag-eval': local sentence-transformers (default), fake, or EMBEDDING_PROVIDER",
    )
    args = parser.parse_args()
    question = " ".join(args.question).strip()
    if args.command == "ask" and not question:
        parser.error('ask needs a question, for example: ask "GA-2100 ka price?"')
    if args.command == "rag-search" and not question:
        parser.error('rag-search needs a question, for example: rag-search "refund policy"')
    try:
        if args.command == "ask":
            asyncio.run(ask(question, debug=args.debug))
        elif args.command == "chat":
            use_selector_event_loop()  # Windows: the checkpointer (psycopg) needs this loop
            asyncio.run(chat(debug=args.debug, checkpoints=not args.no_checkpoint))
        elif args.command == "eval-router":
            asyncio.run(eval_router(args.limit, args.provider))
        elif args.command == "rag-ingest":
            asyncio.run(rag_ingest(args.from_dir))
        elif args.command == "rag-search":
            asyncio.run(rag_search(question, args.en, args.doc_type))
        elif args.command == "rag-eval":
            asyncio.run(rag_eval(args.corpus, args.threshold, not args.no_filter, args.embedder))
        else:
            asyncio.run(COMMANDS[args.command]())
    except AppError as exc:
        print(f"ERROR ({exc.code}): {exc.message}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
