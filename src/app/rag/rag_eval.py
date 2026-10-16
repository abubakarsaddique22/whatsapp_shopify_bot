"""Phase 8, step 5: score retrieval on the golden questions (Blueprint 7.9).

    cases = load_golden()                                   # evals/rag_golden.jsonl
    results = await run_cases(session, embedder, cases)     # one search per question
    print(format_report(results, threshold=0.2))

Golden file: one JSON object per line.

    {"id": "refund-en-01", "lang": "en", "doc_type": "policy_refund",
     "question": "How many days do I have to return a watch?",
     "query_en": "days to return a watch",
     "expect": "within 14 days of delivery"}

- `query_en` is what the router would write (the short English search text). Both it and the
  question are searched, exactly as in the bot.
- `expect` is a phrase that the right passage contains (case does not matter). A phrase is
  better than a chunk id because chunk sizes change while the golden set is tuned.
- A question the documents cannot answer has "doc_type": "none" and no `expect`: the right
  result is an abstention.

Numbers (Blueprint 7.9):
- hit rate: the share of answerable questions whose right passage is among the passages returned;
- MRR: 1 / (position of the right passage), averaged (1.0 = always first);
- abstention accuracy: the share of unanswerable questions that got no passage;
- false abstain: an answerable question that got no passage (the threshold is too high).

Searches run once with no threshold. The threshold is applied afterwards, so one run scores
every threshold (`sweep`) and helps to choose RAG_MIN_SCORE.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.embeddings.base import Embedder
from app.rag.retrieve import MAX_PASSAGES, Passage, candidates, select_passages

ROOT = Path(__file__).resolve().parents[3]
GOLDEN_PATH = ROOT / "evals" / "rag_golden.jsonl"
CORPUS_DIR = ROOT / "evals" / "rag_corpus"
THRESHOLDS = [round(step * 0.05, 2) for step in range(17)]  # 0.00 to 0.80


class GoldenCase(BaseModel):
    id: str
    lang: str  # en | roman_ur
    doc_type: str  # the type of the document with the answer, "none" if there is no answer
    question: str
    query_en: str
    expect: str | None = None

    @property
    def search_type(self) -> str | None:
        """The doc_type filter the router's intent would give. Only the policy intents do."""
        return self.doc_type if self.doc_type.startswith("policy_") else None


@dataclass(frozen=True)
class CaseResult:
    case: GoldenCase
    found: list[Passage]  # fused and ranked, before any threshold


@dataclass
class Group:
    answerable: int = 0
    hits: int = 0
    rr_sum: float = 0.0
    false_abstain: int = 0
    abstain_cases: int = 0
    abstain_correct: int = 0

    @property
    def hit_rate(self) -> float:
        return self.hits / self.answerable if self.answerable else 0.0

    @property
    def mrr(self) -> float:
        return self.rr_sum / self.answerable if self.answerable else 0.0

    @property
    def abstention_accuracy(self) -> float:
        return self.abstain_correct / self.abstain_cases if self.abstain_cases else 0.0

    @property
    def balanced(self) -> float:
        """Hit rate and abstention accuracy together: the number used to pick a threshold."""
        parts: list[float] = []
        if self.answerable:
            parts.append(self.hit_rate)
        if self.abstain_cases:
            parts.append(self.abstention_accuracy)
        return sum(parts) / len(parts) if parts else 0.0


def load_golden(path: Path = GOLDEN_PATH) -> list[GoldenCase]:
    cases: list[GoldenCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(GoldenCase.model_validate(json.loads(line)))
    return cases


async def run_cases(
    session: AsyncSession,
    embedder: Embedder,
    cases: Sequence[GoldenCase],
    *,
    use_filter: bool = True,
    top_k: int | None = None,
) -> list[CaseResult]:
    """Search every question once. `use_filter=False` ignores the doc_type filter."""
    results: list[CaseResult] = []
    for case in cases:
        doc_type = case.search_type if use_filter else None
        found = await candidates(
            session, embedder, [case.query_en, case.question], doc_type, top_k=top_k
        )
        results.append(CaseResult(case, found))
    return results


# ---- scoring


def passage_matches(passage: Passage, expect: str) -> bool:
    haystack = f"{passage.heading_path or ''}\n{passage.text}".lower()
    return expect.lower() in haystack


def _add(group: Group, case: GoldenCase, chosen: Sequence[Passage]) -> None:
    expect = case.expect
    if expect is None:  # nothing in the documents answers it: abstaining is right
        group.abstain_cases += 1
        if not chosen:
            group.abstain_correct += 1
        return
    group.answerable += 1
    if not chosen:
        group.false_abstain += 1
    for position, passage in enumerate(chosen, start=1):
        if passage_matches(passage, expect):
            group.hits += 1
            group.rr_sum += 1.0 / position
            break


def score(results: Sequence[CaseResult], threshold: float) -> dict[str, Group]:
    """Groups: "all", "type:<doc_type>" and "lang:<language>"."""
    groups: dict[str, Group] = {"all": Group()}
    for result in results:
        chosen = select_passages(result.found, threshold, MAX_PASSAGES)
        case = result.case
        for key in ("all", f"type:{case.doc_type}", f"lang:{case.lang}"):
            _add(groups.setdefault(key, Group()), case, chosen)
    return groups


def sweep(
    results: Sequence[CaseResult], thresholds: Sequence[float] = THRESHOLDS
) -> list[tuple[float, Group]]:
    return [(t, score(results, t)["all"]) for t in thresholds]


def best_threshold(rows: Sequence[tuple[float, Group]]) -> float:
    """The threshold with the best balanced score. A tie goes to the lower threshold."""
    best_t, best_value = rows[0][0], -1.0
    for threshold, group in rows:
        if group.balanced > best_value + 1e-12:
            best_t, best_value = threshold, group.balanced
    return best_t


def failures(results: Sequence[CaseResult], threshold: float) -> list[str]:
    """One line for every question that went wrong at this threshold."""
    lines: list[str] = []
    for result in results:
        case = result.case
        chosen = select_passages(result.found, threshold, MAX_PASSAGES)
        if case.expect is None:
            if chosen:
                lines.append(
                    f"  {case.id}: answered but should abstain "
                    f"(best score {chosen[0].score:.3f}): {case.question}"
                )
        elif not chosen:
            best = result.found[0].score if result.found else 0.0
            lines.append(f"  {case.id}: abstained (best score {best:.3f}): {case.question}")
        elif not any(passage_matches(p, case.expect) for p in chosen):
            lines.append(f"  {case.id}: right passage not returned: {case.question}")
    return lines


# ---- report


def _row(name: str, group: Group) -> str:
    abstain = f"{group.abstain_correct}/{group.abstain_cases}"
    return (
        f"{name:<26}{group.answerable:>6}  {group.hit_rate:>8.1%}  {group.mrr:>6.3f}"
        f"  {group.false_abstain:>13}  {abstain:>10}"
    )


def _header() -> str:
    return (
        f"{'group':<26}{'asked':>6}  {'hit rate':>8}  {'MRR':>6}"
        f"  {'false abstain':>13}  {'abstain ok':>10}"
    )


def format_report(results: Sequence[CaseResult], threshold: float) -> str:
    groups = score(results, threshold)
    lines = [
        f"Threshold (RAG_MIN_SCORE): {threshold:.2f}   questions: {len(results)}",
        "",
        _header(),
    ]
    for key in sorted(groups, key=lambda k: (k != "all", k)):
        lines.append(_row(key, groups[key]))

    rows = sweep(results)
    chosen = best_threshold(rows)
    lines += ["", "All thresholds (hit rate, MRR, abstention accuracy, balanced):"]
    for t, group in rows:
        mark = "  <- best" if t == chosen else ""
        lines.append(
            f"  {t:.2f}   hit {group.hit_rate:6.1%}   mrr {group.mrr:.3f}   "
            f"abstain {group.abstention_accuracy:6.1%}   balanced {group.balanced:6.1%}{mark}"
        )

    wrong = failures(results, threshold)
    lines += ["", f"Questions that went wrong at {threshold:.2f}: {len(wrong)}", *wrong]
    return "\n".join(lines)
