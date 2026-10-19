"""Retrieval evaluation (Phase 12, Step 4d; Blueprint 7.9 and 11.4).

Did the knowledge-base search bring back the passage that answers the question? The scoring is
already written and tuned in Phase 8 (rag/rag_eval.py: hit rate, MRR, abstention). This module
adds what the other Phase 12 evaluators have: the blueprint's TARGETS, a gate, a report in the same
shape, and one function that runs the whole thing in a sandbox.

    results = await run(embedder)                    # evals/rag_golden.jsonl vs evals/rag_corpus
    print(format_report(results))
    assert gate_met(results)

The sandbox is the one of `app.cli rag-eval`: the sample documents are loaded inside a transaction
that is rolled back, and every real document is switched off inside it, so the real knowledge base
is never touched or mixed in. Needs Postgres and the local embedding model. No LLM is used.

Gate (Blueprint 11.4, "Retrieval: hit rate and abstention on rag_golden.jsonl, at least 90 % each",
a PROPOSED gate until the client agrees in Phase 12 step 10):

* hit rate at or above TARGET_HIT_RATE, overall AND for every language (a good English score must
  not hide a weak Roman Urdu score);
* abstention accuracy at or above TARGET_ABSTENTION (the share of unanswerable questions that got
  no passage). A threshold alone cannot separate the unanswerable questions that look like real
  ones ('smart watches', 'installments', 'watch straps'): the baseline is 70 %, so this part is
  expected to fail until the composer abstains for them (Phase 9) or the client revises the gate.
  `hit_gate_met` and `abstention_gate_met` give the two parts separately.

MRR is reported only: the blueprint sets no number for it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import get_settings
from app.db.session import get_engine
from app.db.tables import KbDocument
from app.integrations.embeddings.base import Embedder
from app.rag.ingest import ingest_documents, load_sources_from_dir
from app.rag.rag_eval import (
    CORPUS_DIR,
    CaseResult,
    GoldenCase,
    Group,
    best_threshold,
    failures,
    load_golden,
    run_cases,
    score,
    sweep,
)

TARGET_HIT_RATE = 0.90  # blueprint 11.4 (proposed): answerable questions whose passage was found
TARGET_ABSTENTION = 0.90  # blueprint 11.4 (proposed): unanswerable questions that got no passage

__all__ = [
    "TARGET_ABSTENTION",
    "TARGET_HIT_RATE",
    "Summary",
    "abstention_gate_met",
    "format_report",
    "gate_met",
    "hit_gate_met",
    "load_golden",
    "run",
    "summarize",
]


async def run(
    embedder: Embedder,
    *,
    use_filter: bool = True,
    corpus: Path = CORPUS_DIR,
    cases: Sequence[GoldenCase] | None = None,
) -> list[CaseResult]:
    """Search every golden question once, against the sample documents only.

    The threshold is applied afterwards (summarize / format_report), so one run scores any
    threshold. `cases=None` means the whole of evals/rag_golden.jsonl.
    """
    docs = load_sources_from_dir(corpus)
    chosen = list(cases) if cases is not None else load_golden()
    engine = get_engine()
    async with engine.connect() as conn:
        transaction = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            await session.execute(update(KbDocument).values(is_active=False))
            await ingest_documents(session, embedder, docs, managed_prefixes=("file:",))
            return await run_cases(session, embedder, chosen, use_filter=use_filter)
        finally:
            await session.close()
            await transaction.rollback()


# ---- the numbers


@dataclass
class Summary:
    threshold: float
    overall: Group
    by_lang: dict[str, Group]
    by_type: dict[str, Group]
    wrong: list[str]  # one line per question that went wrong (from rag_eval.failures)


def summarize(results: Sequence[CaseResult], threshold: float | None = None) -> Summary:
    level = get_settings().rag_min_score if threshold is None else threshold
    groups = score(results, level)
    return Summary(
        threshold=level,
        overall=groups["all"],
        by_lang={k.removeprefix("lang:"): g for k, g in groups.items() if k.startswith("lang:")},
        by_type={k.removeprefix("type:"): g for k, g in groups.items() if k.startswith("type:")},
        wrong=failures(results, level),
    )


def hit_gate_met(results: Sequence[CaseResult], threshold: float | None = None) -> bool:
    """Hit rate at the target overall and for every language."""
    s = summarize(results, threshold)
    if not s.overall.answerable:  # nothing answerable was asked: nothing was measured
        return False
    per_language = all(g.hit_rate >= TARGET_HIT_RATE for g in s.by_lang.values() if g.answerable)
    return s.overall.hit_rate >= TARGET_HIT_RATE and per_language


def abstention_gate_met(results: Sequence[CaseResult], threshold: float | None = None) -> bool:
    """Abstention accuracy at the target. No unanswerable question asked: nothing to fail."""
    overall = summarize(results, threshold).overall
    return not overall.abstain_cases or overall.abstention_accuracy >= TARGET_ABSTENTION


def gate_met(results: Sequence[CaseResult], threshold: float | None = None) -> bool:
    return hit_gate_met(results, threshold) and abstention_gate_met(results, threshold)


# ---- the report


def _pct(value: float) -> str:
    return f"{value:.1%}"


def _line(name: str, group: Group) -> str:
    return (
        f"  {name:<18} asked {group.answerable:>3}  hit {_pct(group.hit_rate):>6}  "
        f"MRR {group.mrr:.3f}  false abstain {group.false_abstain:>2}  "
        f"abstain ok {group.abstain_correct}/{group.abstain_cases}"
    )


def _verdict(ok: bool) -> str:
    return "MET" if ok else "NOT MET"


def format_report(results: Sequence[CaseResult], threshold: float | None = None) -> str:
    s = summarize(results, threshold)
    o = s.overall
    lines = [
        f"Retrieval: {len(results)} golden questions, RAG_MIN_SCORE {s.threshold:.2f}",
        f"  hit rate:   {_pct(o.hit_rate)}   (target {_pct(TARGET_HIT_RATE)}, every language too)",
        f"  abstention: {_pct(o.abstention_accuracy)}   "
        f"({o.abstain_correct}/{o.abstain_cases} unanswerable questions got no passage, "
        f"target {_pct(TARGET_ABSTENTION)})",
        f"  MRR:        {o.mrr:.3f}   (reported only, no target); false abstain {o.false_abstain}",
        "\nBy language:",
        *(_line(name, g) for name, g in sorted(s.by_lang.items())),
        "\nBy document type:",
        *(_line(name, g) for name, g in sorted(s.by_type.items())),
    ]
    rows = sweep(results)
    best = best_threshold(rows)
    lines.append(
        f"\nBest threshold of the sweep (balanced hit + abstention): {best:.2f}"
        f"{'  (the one in use)' if abs(best - s.threshold) < 1e-9 else ''}"
    )
    if s.wrong:
        lines.append(f"\nWent wrong at {s.threshold:.2f} ({len(s.wrong)}):")
        lines.extend(s.wrong)
    lines.append(
        f"\nGate (blueprint 11.4, proposed): hit rate {_verdict(hit_gate_met(results, threshold))}"
        f", abstention {_verdict(abstention_gate_met(results, threshold))}. "
        f"Overall {_verdict(gate_met(results, threshold))}"
    )
    return "\n".join(lines)
