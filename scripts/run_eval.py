r"""Phase 12: ask the REAL bot every question of an evaluation set and score the answers.

    set PYTHONPATH=src
    uv run python scripts\run_eval.py
    uv run python scripts\run_eval.py --kind price --limit 5
    uv run python scripts\run_eval.py --set retrieval
    uv run python scripts\run_eval.py --delay 2
    uv run python scripts\run_eval.py --set duplicates   (T16 + chaos, stop the worker first)
    uv run python scripts\run_eval.py --set latency      (webhook to outbox, stop the worker first)
    uv run python scripts\run_eval.py --all --delay 3    (every gate, one summary at the end)

Needs LLM_* and COMPOSER_MODEL in .env, Postgres running (docker compose up -d), the real dev
store. Nothing is sent to WhatsApp. Every question starts a NEW conversation (state reset, no
checkpointer), so one answer never helps the next one. Exit code 1 if any answer is wrong.

Asking the bot lives in src/app/evals/runner.py (shared with tests/llm).
"""

import argparse
import asyncio
import copy
import io
import sys
from typing import Any

from app.agent.graph import use_selector_event_loop
from app.core.errors import ConfigError
from app.db.session import get_engine, get_sessionmaker
from app.evals import (
    duplicates,
    language,
    latency,
    pipeline,
    product_qa,
    retrieval,
    runner,
    security,
    tools,
)
from app.integrations.embeddings.factory import get_eval_embedder
from app.integrations.llm.chat_factory import get_chat_model, get_eval_chat_model
from app.integrations.llm.fake_llm import FakeLLM
from app.worker import POLL_SECONDS

LATENCY_SAMPLE = 30  # questions timed when --limit is not given
ALL_SETS = (
    "golden",
    "natural",
    "tools",
    "language",
    "retrieval",
    "security",
    "duplicates",
    "latency",
)


def deps_args_or_exit(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return runner.build_deps_args(ollama=args.ollama)
    except ConfigError as exc:
        raise SystemExit(f"run_eval: {exc.message}") from exc


async def run_security(args: argparse.Namespace) -> int:
    """evals/adversarial.jsonl: the real router on every attack, the scanner on every store text.

    No database and no store needed. The router model is Ollama with --ollama (the model rule in
    docs/PROGRESS.md), otherwise the app's router model on Groq.
    """
    rows = security.load_rows()
    customers = security.customer_rows(rows)
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        customers = [r for r in customers if r.id in wanted]
    llm = get_eval_chat_model() if args.ollama else get_chat_model("router")
    if isinstance(llm, FakeLLM):
        raise SystemExit("run_eval needs a real model: set LLM_PROVIDER=groq in .env")
    print(f"Security: {len(customers)} attack messages through the real router\n")
    results = await security.run_router(customers, llm, limit=args.limit, delay=args.delay)
    docs = security.scan_documents(security.document_rows(rows))
    print(security.format_report(results, docs))
    return 0 if security.gate_met(results, docs) else 1


async def run_language(args: argparse.Namespace) -> int:
    """evals/router_golden.jsonl through the whole bot: router language and reply language.

    Every message is a new conversation. Needs Postgres and the real model, no store questions.
    """
    cases = language.load_golden()
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        cases = [c for c in cases if c.id in wanted]
    cases = cases[: args.limit]
    deps_args = deps_args_or_exit(args)
    print(f"Language: {len(cases)} messages, real model, real store\n")
    results = []
    try:
        with runner.watching_provider_errors() as catcher:
            for number, case in enumerate(cases, start=1):
                run = await runner.ask_with_retry(deps_args, [case.text], catcher)
                result = language.LanguageResult(case, run.router_language, run.reply)
                results.append(result)
                mark = "ok" if result.router_ok and result.reply_ok else "WRONG"
                print(f"[{number}/{len(cases)}] {case.id} {mark}")
                await asyncio.sleep(args.delay)
    finally:
        await get_engine().dispose()
    print()
    print(language.format_report(results))
    return 0 if language.gate_met(results) else 1


async def run_tools(args: argparse.Namespace) -> int:
    """Right tool for the message: product, policy and attack cases (src/app/evals/tools.py).

    --limit means the first N cases of EACH group. Every case is a fresh conversation.
    """
    cases = tools.build_cases(args.limit)
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        cases = [c for c in cases if c.id in wanted]
    deps_args = deps_args_or_exit(args)
    print(f"Tools: {len(cases)} cases, real model, real store\n")
    outcomes = []
    try:
        with runner.watching_provider_errors() as catcher:
            for number, case in enumerate(cases, start=1):
                run = await runner.ask_with_retry(deps_args, case.texts, catcher)
                outcome = tools.ToolOutcome(case, run.tools, run.reply, run.intent)
                outcomes.append(outcome)
                mark = "ok" if outcome.ok else "WRONG"
                print(f"[{number}/{len(cases)}] {case.id} {mark}")
                await asyncio.sleep(args.delay)
    finally:
        await get_engine().dispose()
    print()
    print(tools.format_report(outcomes))
    return 0 if tools.gate_met(outcomes) else 1


async def run_retrieval(args: argparse.Namespace) -> int:
    """evals/rag_golden.jsonl against the sample documents in evals/rag_corpus (retrieval.py).

    No LLM and no store: Postgres and the local embedding model only. The sample documents live in
    a transaction that is rolled back, the real knowledge base is not touched.
    """
    cases = retrieval.load_golden()
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        cases = [c for c in cases if c.id in wanted]
    cases = cases[: args.limit]
    embedder = get_eval_embedder()
    print(
        f"Retrieval: {len(cases)} golden questions, "
        f"embedder {embedder.name} / {embedder.model}\n"
    )
    try:
        results = await retrieval.run(embedder, cases=cases)
    finally:
        await get_engine().dispose()
    print(retrieval.format_report(results, args.threshold))
    return 0 if retrieval.gate_met(results, args.threshold) else 1


async def run_duplicates(args: argparse.Namespace) -> int:
    """T16 and the chaos scenarios (src/app/evals/pipeline.py, duplicates.py).

    The REAL worker and sender on the real Postgres, a fake Meta, an empty FakeLLM: no model and
    no store are used, nothing is sent to WhatsApp. It COMMITS rows of fake customers (and deletes
    them again), so the worker must be stopped and the queue empty: the run refuses otherwise.
    """
    print(f"Duplicates: {len(pipeline.SCENARIOS)} scenarios, real worker, fake Meta\n")

    def show(result: Any) -> None:
        mark = "ERROR" if result.error else ("ok" if result.ok else "WRONG")
        gap = " (known gap)" if result.known_gap else ""
        print(f"  {result.name} {mark}{gap}")

    try:
        results = await pipeline.run_duplicate_scenarios(get_sessionmaker(), on_result=show)
    except ConfigError as exc:
        raise SystemExit(f"run_eval: {exc.message}") from exc
    finally:
        await get_engine().dispose()
    print()
    print(duplicates.format_report(results))
    return 0 if duplicates.gate_met(results) else 1


async def run_latency(args: argparse.Namespace) -> int:
    """Webhook to outbox for text (src/app/evals/latency.py): the real worker code, the app's real
    models (Groq), the real dev store, one fresh customer per question.

    Needs Postgres, LLM_* in .env and the worker stopped. --ollama is refused: how fast Ollama
    is says nothing about Groq. Voice is timed separately later (needs the baseline clips).
    """
    if args.ollama:
        raise SystemExit("run_eval: latency counts only on the app's own models: no --ollama")
    deps_args_or_exit(args)  # a real model must be set up, not the FakeLLM
    rows = [
        r
        for r in product_qa.load_rows()
        if r["kind"] in ("price", "stock") and not r.get("setup")
    ]
    wanted = args.limit or LATENCY_SAMPLE
    step = max(1, len(rows) // wanted)  # spread the sample over price and stock questions
    questions = [(r["id"], r["question"]) for r in rows[::step][:wanted]]
    if not questions:
        raise SystemExit("run_eval: evals/product_qa.jsonl has no price or stock question")
    print(f"Latency: {len(questions)} questions through the real worker, real models\n")

    def show(number: int, total: int, run: latency.LatencyRun) -> None:
        mark = f"{run.seconds:.1f} s" if run.ok else f"not counted ({run.note})"
        print(f"[{number}/{total}] {run.id} {mark}")

    try:
        runs = await pipeline.measure_text(
            get_sessionmaker(), questions, delay=args.delay, on_run=show
        )
    except ConfigError as exc:
        raise SystemExit(f"run_eval: {exc.message}") from exc
    finally:
        await get_engine().dispose()
    print()
    voice_clips = latency.voice_clip_count()
    print(latency.format_report(runs, voice_clips=voice_clips, poll_allowance=POLL_SECONDS))
    return 0 if latency.gate_met(runs, POLL_SECONDS) else 1


async def run_all(args: argparse.Namespace) -> int:
    """Every evaluator one after the other, then one table. Exit code 1 if any gate is NOT MET.

    A set that cannot start (no model, queue not empty) or crashes counts as NOT MET, so the
    table never shows a green line for a gate that was not checked. --limit and --delay apply to
    every set. With --ollama the latency set is SKIPPED (it only counts on the app's models).
    """
    if args.ids or args.kind:
        raise SystemExit("run_eval: --all runs every set; --kind and --ids belong to one set")
    summary: list[tuple[str, str]] = []
    for name in ALL_SETS:
        print(f"\n{'=' * 72}\n{name.upper()}\n{'=' * 72}")
        if name == "latency" and args.ollama:
            print("skipped: latency counts only on the app's own models (run without --ollama)")
            summary.append((name, "SKIPPED"))
            continue
        sub = copy.copy(args)
        sub.set = name
        sub.all = False
        try:
            code = await run(sub)
        except SystemExit as exc:  # a set that could not start says why in exc.code
            print(exc.code if isinstance(exc.code, str) else f"stopped (exit {exc.code})")
            code = 1
        except Exception as exc:  # one broken evaluator must not hide the others
            print(f"crashed: {type(exc).__name__}: {exc}")
            code = 1
        summary.append((name, "MET" if code == 0 else "NOT MET"))
    print(f"\n{'=' * 72}\nSUMMARY (blueprint 11.4 gates)\n{'=' * 72}")
    for name, verdict in summary:
        print(f"  {name:<12} {verdict}")
    failed = [name for name, verdict in summary if verdict == "NOT MET"]
    print("\n" + (f"NOT MET: {', '.join(failed)}" if failed else "All gates MET"))
    return 1 if failed else 0


async def run(args: argparse.Namespace) -> int:
    if args.all:
        return await run_all(args)
    if args.set == "duplicates":
        return await run_duplicates(args)
    if args.set == "latency":
        return await run_latency(args)
    if args.set == "retrieval":
        return await run_retrieval(args)
    if args.set == "security":
        return await run_security(args)
    if args.set == "language":
        return await run_language(args)
    if args.set == "tools":
        return await run_tools(args)
    natural = args.set == "natural"
    rows = product_qa.load_rows(product_qa.NATURAL if natural else product_qa.DATA)
    vocab = product_qa.vocabulary(rows)
    chosen = product_qa.select(rows, args.kind, args.limit)
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        chosen = [r for r in rows if r["id"] in wanted]
    deps_args = deps_args_or_exit(args)
    label = "Natural" if natural else "Product"
    print(f"{label} Q&A: {len(chosen)} questions, real model, real store\n")
    results = []
    try:
        with runner.watching_provider_errors() as catcher:
            for number, row in enumerate(chosen, start=1):
                texts = [*row.get("setup", []), row["question"]]
                run = await runner.ask_with_retry(deps_args, texts, catcher)
                if natural:
                    result = product_qa.score_natural(row, run.reply)
                    mark = {"correct": "ok", "clarified": "asked"}.get(result.verdict, "WRONG")
                else:
                    result = product_qa.score(row, run.reply, vocab)
                    mark = "ok" if result.ok else "WRONG"
                results.append(result)
                print(f"[{number}/{len(chosen)}] {row['id']} {mark}")
                if not result.ok and run.errors:
                    print(f"      tool errors: {', '.join(run.errors)}")
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
        choices=[
            "golden",
            "natural",
            "security",
            "language",
            "tools",
            "retrieval",
            "duplicates",
            "latency",
        ],
        default="golden",
        help="golden: exact titles, every answer must be right. natural: messy questions, "
        "no wrong fact allowed (asking is fine). security: evals/adversarial.jsonl, the real "
        "router must recognise every attack. language: evals/router_golden.jsonl, router and "
        "reply language. tools: the right tool ran (product, policy and attack cases). "
        "retrieval: evals/rag_golden.jsonl, hit rate and MRR (no LLM). duplicates: T16 and "
        "chaos scenarios, 0 duplicate replies (real worker, fake Meta, no model). latency: "
        "webhook to outbox, text p50 / p95 (real worker, app's real models)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="run every evaluator one after the other and print one summary (the worker must "
        "be stopped: duplicates and latency use the real queue)",
    )
    parser.add_argument(
        "--threshold", type=float, help="retrieval set: RAG_MIN_SCORE to score (default: .env)"
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
