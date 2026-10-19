"""Language evaluation (Phase 12, Step 4b; Blueprint 11.4: language above 95 %).

Two numbers, both measured on the same messages (evals/router_golden.jsonl, one fresh
conversation per message, the REAL bot):

* router language: did the router name the language the customer wrote? (router_output.language)
* reply language:  is the reply in the language the policy requires? The required language comes
  from the CASE language (reply_language(case.language)), not from what the router said, so a
  router mistake that changes the reply language is counted twice, as it should be. The check is
  the app's own cheap one (language/policy.reply_matches).

This module only scores. Asking the bot is done by scripts/run_eval.py (--set language), which
passes in the reply text and the router's language. No model and no database are used here.

    result = LanguageResult(case, router_language, reply)
    print(format_report(results))
"""

from dataclasses import dataclass

from app.agent.router_eval import GoldenCase, load_golden
from app.domain.models import Lang
from app.language.policy import ReplyLang, reply_language, reply_matches

TARGET = 95.0  # percent, blueprint 11.4 (both numbers)
TEXT_CLIP = 70

__all__ = ["TARGET", "LanguageResult", "format_report", "gate_met", "load_golden", "summarize"]


@dataclass
class LanguageResult:
    case: GoldenCase
    router_language: Lang | None  # None = the router gave nothing (crash, provider failure)
    reply: str  # "" = no text reply (for example pictures only): not scored

    @property
    def expected_reply(self) -> ReplyLang:
        return reply_language(self.case.language)

    @property
    def router_ok(self) -> bool:
        return self.router_language == self.case.language

    @property
    def scored_reply(self) -> bool:
        return bool(self.reply.strip()) and not self.reply.startswith("[")  # [ERROR ...] rows

    @property
    def reply_ok(self) -> bool:
        return reply_matches(self.reply, self.expected_reply)


def _pct(hits: int, total: int) -> float | None:
    return round(100 * hits / total, 1) if total else None


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value}%"


def summarize(results: list[LanguageResult]) -> dict:
    replies = [r for r in results if r.scored_reply]
    by_class: dict[str, list[LanguageResult]] = {}
    for r in results:
        by_class.setdefault(r.case.language.value, []).append(r)
    return {
        "total": len(results),
        "router_pct": _pct(sum(r.router_ok for r in results), len(results)),
        "scored_replies": len(replies),
        "reply_pct": _pct(sum(r.reply_ok for r in replies), len(replies)),
        "by_class": {
            name: (
                len(rows),
                _pct(sum(r.router_ok for r in rows), len(rows)),
                _pct(
                    sum(r.reply_ok for r in rows if r.scored_reply),
                    sum(r.scored_reply for r in rows),
                ),
            )
            for name, rows in sorted(by_class.items())
        },
        "router_wrong": [r for r in results if not r.router_ok],
        "reply_wrong": [r for r in replies if not r.reply_ok],
    }


def gate_met(results: list[LanguageResult]) -> bool:
    s = summarize(results)
    router, reply = s["router_pct"], s["reply_pct"]
    return router is not None and reply is not None and router >= TARGET and reply >= TARGET


def format_report(results: list[LanguageResult]) -> str:
    s = summarize(results)
    lines = [
        f"Language: {s['total']} messages through the real bot",
        f"  router language: {_fmt(s['router_pct'])}   (target {TARGET:.0f}%)",
        f"  reply language:  {_fmt(s['reply_pct'])}   ({s['scored_replies']} text replies, "
        f"target {TARGET:.0f}%)",
        "\nBy language class (n / router / reply):",
    ]
    for name, (count, router, reply) in s["by_class"].items():
        lines.append(f"  {name:<9} n={count:<3} {_fmt(router):>7} {_fmt(reply):>7}")
    if s["router_wrong"]:
        lines.append(f"\nRouter named another language ({len(s['router_wrong'])}):")
        for r in s["router_wrong"]:
            got = r.router_language.value if r.router_language else "none"
            lines.append(
                f"  {r.case.id} {r.case.text[:TEXT_CLIP]!r}  expected {r.case.language.value}, "
                f"got {got}"
            )
    if s["reply_wrong"]:
        lines.append(f"\nReply in the wrong language ({len(s['reply_wrong'])}):")
        for r in s["reply_wrong"]:
            lines.append(f"  {r.case.id} needed {r.expected_reply}: {r.reply[:TEXT_CLIP]!r}")
    verdict = "MET" if gate_met(results) else "NOT MET"
    lines.append(f"\nGate (blueprint 11.4): language {TARGET:.0f}%+ on both. {verdict}")
    return "\n".join(lines)
