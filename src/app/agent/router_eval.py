"""Phase 5, step 5: score the router on evals/router_golden.jsonl.

    cases = load_golden()
    results = await run_eval(cases, llm)      # llm = the REAL model for a real baseline
    print(format_report(summarize(results)))

Run from the command line with:  uv run python -m app.cli eval-router

The scoring (summarize, format_report) is plain arithmetic, so it is tested with a FakeLLM.
The numbers only mean something when the real model is used and the golden set was written
or reviewed by a native speaker (rows marked "synthetic-starter" were not).
"""

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.agent.router import route_intent
from app.domain.models import Intent, Lang, RouterOutput
from app.integrations.llm.base import ChatModel
from app.language.detect import detect_language

GOLDEN_PATH = Path(__file__).resolve().parents[3] / "evals" / "router_golden.jsonl"
SYNTHETIC = "synthetic-starter"


class GoldenCase(BaseModel):
    id: str
    text: str
    language: Lang
    intent: Intent
    color: str | None = None
    max_price: int | None = None
    source: str = "unknown"


@dataclass
class CaseResult:
    case: GoldenCase
    got: RouterOutput
    hint: Lang  # what the cheap word check (no model) said

    @property
    def wrong(self) -> list[str]:
        """Names of the fields the router got wrong (empty list = fully correct)."""
        expected_color = self.case.color.lower() if self.case.color else None
        got_color = self.got.color.strip().lower() if self.got.color else None
        checks = {
            "language": self.got.language == self.case.language,
            "intent": self.got.intent == self.case.intent,
            "color": got_color == expected_color,
            "max_price": self.got.max_price == self.case.max_price,
        }
        return [name for name, ok in checks.items() if not ok]

    @property
    def fell_back(self) -> bool:
        """True when the router gave up and returned its safe 'unclear' answer."""
        got = self.got
        return got.intent == Intent.UNCLEAR and got.needs_clarification and got.confidence == 0.0


def load_golden(path: Path = GOLDEN_PATH) -> list[GoldenCase]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    cases = [GoldenCase.model_validate(json.loads(line)) for line in lines]
    duplicates = [i for i, n in Counter(c.id for c in cases).items() if n > 1]
    if duplicates:
        raise ValueError(f"duplicate ids in {path.name}: {duplicates}")
    return cases


async def run_eval(
    cases: list[GoldenCase], llm: ChatModel, limit: int | None = None
) -> list[CaseResult]:
    """One router call per case, one after the other (kind to the provider's rate limit)."""
    results = []
    for case in cases[:limit]:
        got = await route_intent(case.text, llm)
        results.append(CaseResult(case, got, detect_language(case.text).language))
    return results


def _pct(hits: int, total: int) -> float | None:
    return round(100 * hits / total, 1) if total else None


def _right(rows: list[CaseResult], field: str) -> float | None:
    """Percent of rows where `field` was not wrong."""
    return _pct(sum(field not in r.wrong for r in rows), len(rows))


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    total = len(results)
    with_color = [r for r in results if r.case.color or r.got.color]
    with_price = [
        r for r in results if r.case.max_price is not None or r.got.max_price is not None
    ]

    groups: defaultdict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        groups[r.case.language.value].append(r)
    by_language = {
        language: {
            "cases": len(rows),
            "language_pct": _right(rows, "language"),
            "intent_pct": _right(rows, "intent"),
        }
        for language, rows in sorted(groups.items())
    }

    return {
        "total": total,
        "language_pct": _right(results, "language"),
        "intent_pct": _right(results, "intent"),
        "hint_language_pct": _pct(sum(r.hint == r.case.language for r in results), total),
        "color_pct": _right(with_color, "color"),
        "max_price_pct": _right(with_price, "max_price"),
        "fallbacks": sum(r.fell_back for r in results),
        "sources": dict(Counter(r.case.source for r in results)),
        "by_language": by_language,
        "failures": [
            {
                "id": r.case.id,
                "text": r.case.text,
                "wrong": r.wrong,
                "expected": f"{r.case.language.value} / {r.case.intent.value} / "
                f"color={r.case.color} / max_price={r.case.max_price}",
                "got": f"{r.got.language.value} / {r.got.intent.value} / "
                f"color={r.got.color} / max_price={r.got.max_price}",
            }
            for r in results
            if r.wrong
        ],
    }


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value}%"


def format_report(summary: dict[str, Any]) -> str:
    lines = [
        f"Cases:                     {summary['total']}",
        f"Language match:            {_fmt(summary['language_pct'])}   (target 95%, proposed)",
        f"Intent match:              {_fmt(summary['intent_pct'])}",
        f"Colour match:              {_fmt(summary['color_pct'])}",
        f"max_price match:           {_fmt(summary['max_price_pct'])}",
        f"Word-check language only:  {_fmt(summary['hint_language_pct'])}   (no model)",
        f"Fell back to 'unclear':    {summary['fallbacks']}",
        "",
        "By language class (language / intent):",
    ]
    for language, row in summary["by_language"].items():
        lines.append(
            f"  {language:<9} n={row['cases']:<3} {_fmt(row['language_pct']):>7} "
            f"{_fmt(row['intent_pct']):>7}"
        )
    if SYNTHETIC in summary["sources"]:
        lines += [
            "",
            f"NOTE: {summary['sources'][SYNTHETIC]} of {summary['total']} cases are "
            f"'{SYNTHETIC}'. This is not a real baseline until a native speaker has "
            "reviewed or replaced them.",
        ]
    if summary["failures"]:
        lines += ["", f"Wrong ({len(summary['failures'])}):"]
        for f in summary["failures"]:
            lines += [
                f"  {f['id']}  {f['text']!r}  <- {', '.join(f['wrong'])}",
                f"      expected: {f['expected']}",
                f"      got:      {f['got']}",
            ]
    return "\n".join(lines)
