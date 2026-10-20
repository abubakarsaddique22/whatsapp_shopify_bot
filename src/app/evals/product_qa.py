"""Product Q&A evaluation set (Phase 12, Step 3): load, score and report. No bot, no network.

scripts/run_eval.py asks the real bot each question in evals/product_qa.jsonl; this module reads
the file, scores each reply with the exact-fact evaluators and prints the summary.
"""

import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.evals.evaluators import check_row, grade_natural

DATA = Path("evals") / "product_qa.jsonl"
NATURAL = Path("evals") / "natural_qa.jsonl"
REPLY_CLIP = 220  # characters of a wrong reply shown in the report


class Result(BaseModel):
    id: str
    kind: str
    lang: str
    question: str
    reply: str
    ok: bool
    reason: str = ""
    style: str = ""  # natural set only: how the question is written (typo, short, followup, ...)
    verdict: str = ""  # natural set only: correct | clarified | wrong | error


def load_rows(path: Path = DATA) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def select(rows: list[dict[str, Any]], kind: str | None, limit: int | None) -> list[dict[str, Any]]:
    """Only one kind and/or the first N rows. The vocabulary is built from ALL rows first."""
    chosen = [r for r in rows if kind is None or r["kind"] == kind]
    return chosen[:limit] if limit else chosen


def vocabulary(rows: Iterable[dict[str, Any]]) -> dict[str, set[str]]:
    """Every colour (and every size) in the file, per kind. A reply for a product that has only
    Black and Blue must not mention a colour from this list that the product does not have."""
    words: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row["kind"] in ("colors", "sizes"):
            words[row["kind"]].update(str(v) for v in row["expected"]["values"])
    return dict(words)


def score(row: dict[str, Any], reply: str, vocab: dict[str, set[str]]) -> Result:
    check = check_row(row, reply, forbidden=vocab.get(row["kind"], set()))
    reason = check.reason
    if not check.ok and reply.startswith(("[PROVIDER FAILED", "[ERROR")):
        reason = "RUN ERROR (provider limit or crash), not a wrong fact: run this question again"
    return Result(
        id=row["id"],
        kind=row["kind"],
        lang=row["lang"],
        question=row["question"],
        reply=reply,
        ok=check.ok,
        reason=reason,
    )


def score_natural(row: dict[str, Any], reply: str) -> Result:
    """ok = the bot did not state a wrong fact (a right fact and an honest question both pass)."""
    graded = grade_natural(row, reply)
    return Result(
        id=row["id"],
        kind=row["kind"],
        lang=row["lang"],
        question=row["question"],
        reply=reply,
        ok=graded.verdict in ("correct", "clarified"),
        reason=graded.reason,
        style=row.get("style", ""),
        verdict=graded.verdict,
    )


def format_natural_report(results: list[Result]) -> str:
    total = len(results)
    count: dict[str, int] = {}
    for name in ("correct", "clarified", "wrong", "error"):
        count[name] = sum(r.verdict == name for r in results)
    lines = [
        f"Natural Q&A: {total} messy questions",
        f"  correct fact      {count['correct']:>3}  {_percent(count['correct'], total)}"
        "   <- answer rate (should grow)",
        f"  asked / no fact   {count['clarified']:>3}  {_percent(count['clarified'], total)}"
        "   <- allowed, but check it is a sensible question",
        f"  WRONG fact        {count['wrong']:>3}  {_percent(count['wrong'], total)}"
        "   <- must be 0",
        f"  run errors        {count['error']:>3}",
        "\nBy style (correct / asked / wrong):",
    ]
    for style in sorted({r.style for r in results}):
        group = [r for r in results if r.style == style]
        c, a, w = (sum(r.verdict == v for r in group) for v in ("correct", "clarified", "wrong"))
        lines.append(f"  {style:<12} {c}/{a}/{w}  (of {len(group)})")
    bad = [r for r in results if r.verdict in ("wrong", "error")]
    asked = [r for r in results if r.verdict == "clarified"]
    if asked:  # not a failure, but each one is a question the bot could maybe have answered
        lines.append(f"\nAsked instead of answering ({len(asked)}):")
        for r in asked:
            reply = r.reply.replace("\n", " ")[:REPLY_CLIP]
            lines.append(f"  {r.id} [{r.style}] {r.question}")
            lines.append(f"      reply: {reply}")
    if bad:
        lines.append(f"\nWrong or failed ({len(bad)}):")
        for r in bad:
            reply = r.reply.replace("\n", " ")[:REPLY_CLIP]
            lines.append(f"  {r.id} [{r.style}] {r.question}")
            lines.append(f"      why:   {r.reason}")
            lines.append(f"      reply: {reply}")
    lines.append("\nGate: wrong facts = 0. " + ("NOT MET" if bad else "MET"))
    return "\n".join(lines)


def tally(results: Iterable[Result], field: str) -> dict[str, tuple[int, int]]:
    """{'price': (passed, total), ...} grouped by 'kind' or 'lang'."""
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for result in results:
        key = getattr(result, field)
        counts[key][1] += 1
        counts[key][0] += int(result.ok)
    return {key: (done, total) for key, (done, total) in sorted(counts.items())}


def _percent(done: int, total: int) -> str:
    return f"{100 * done / total:.1f}%" if total else "-"


def format_report(results: list[Result]) -> str:
    passed = sum(r.ok for r in results)
    lines = [f"Product Q&A: {passed}/{len(results)} passed ({_percent(passed, len(results))})"]
    for field, title in (("kind", "By kind"), ("lang", "By language")):
        lines.append(f"\n{title}:")
        for key, (done, total) in tally(results, field).items():
            lines.append(f"  {key:<10} {done}/{total}  {_percent(done, total)}")
    failed = [r for r in results if not r.ok]
    if failed:
        lines.append(f"\nWrong ({len(failed)}):")
        for r in failed:
            reply = r.reply.replace("\n", " ")[:REPLY_CLIP]
            lines.append(f"  {r.id} [{r.lang}] {r.question}")
            lines.append(f"      why:   {r.reason}")
            lines.append(f"      reply: {reply}")
    lines.append("\nGate (blueprint 11.4): facts 100%. " + ("MET" if not failed else "NOT MET"))
    return "\n".join(lines)
