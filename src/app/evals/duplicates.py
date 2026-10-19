"""Duplicate-reply evaluation (Phase 12, Step 4f; Blueprint 11.4: T16 and a chaos test, 0 duplicate
replies).

T16 (blueprint table 11.2): "a webhook event is delivered twice: one inbox row, one reply".
The chaos test: the same, but with things going wrong around it (Meta redelivers at the same
moment, the worker dies before its commit, two workers race for the queue, Meta answers 5xx and
the sender retries).

This module only scores. `pipeline.run_duplicate_scenarios` (scripts/run_eval.py --set duplicates)
drives the REAL worker and sender on the real database with a fake Meta and an empty FakeLLM (no
real model: the reply text does not matter, the NUMBER of replies does), and hands in one
`Tally` per customer:

    wamids  how many different messages the customer sent
    inbox   rows in whatsapp_inbox          (more than wamids = a duplicate got stored)
    queued  rows in whatsapp_outbox         ((inbox_id, seq) is unique: a retry cannot add more)
    sent    messages the fake Meta accepted (more than queued = a reply went out twice)

    duplicate replies = sent - queued   (the customer saw the same reply again)
    duplicate inbox   = inbox - wamids
    lost replies      = a message that did not get its reply

A scenario can be marked `known_gap`: a hole that is written down and shown in the report, but not
counted in the gate (see pipeline.SCENARIOS). The gate needs every other scenario at 0 duplicates,
0 lost replies and no error.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class Tally:
    wamids: int
    inbox: int
    queued: int
    sent: int

    @property
    def duplicate_inbox(self) -> int:
        return max(0, self.inbox - self.wamids)

    @property
    def duplicate_replies(self) -> int:
        return max(0, self.sent - self.queued)

    @property
    def lost_replies(self) -> int:
        return max(0, max(self.queued, self.wamids) - self.sent)


@dataclass
class ScenarioResult:
    name: str
    what: str
    deliveries: int  # how many times each message was delivered (Meta retries)
    tallies: dict[str, Tally] = field(default_factory=dict)  # one per customer
    known_gap: bool = False
    error: str = ""  # the scenario itself crashed: not a result about the bot

    @property
    def customers(self) -> int:
        return len(self.tallies)

    @property
    def inbox(self) -> int:
        return sum(t.inbox for t in self.tallies.values())

    @property
    def sent(self) -> int:
        return sum(t.sent for t in self.tallies.values())

    @property
    def duplicate_inbox(self) -> int:
        return sum(t.duplicate_inbox for t in self.tallies.values())

    @property
    def duplicate_replies(self) -> int:
        return sum(t.duplicate_replies for t in self.tallies.values())

    @property
    def lost_replies(self) -> int:
        return sum(t.lost_replies for t in self.tallies.values())

    @property
    def ok(self) -> bool:
        return (
            not self.error
            and bool(self.tallies)
            and self.duplicate_inbox == 0
            and self.duplicate_replies == 0
            and self.lost_replies == 0
        )


def gate_met(results: Sequence[ScenarioResult]) -> bool:
    """Every counted scenario clean. A known gap is shown, not counted. No scenario = not met."""
    counted = [r for r in results if not r.known_gap]
    return bool(counted) and all(r.ok for r in counted)


def totals(results: Sequence[ScenarioResult]) -> tuple[int, int]:
    """(duplicate replies, duplicate inbox rows) over the counted scenarios."""
    counted = [r for r in results if not r.known_gap]
    return (
        sum(r.duplicate_replies for r in counted),
        sum(r.duplicate_inbox for r in counted),
    )


def _row(r: ScenarioResult) -> str:
    if r.error:
        return f"  {r.name:<24} ERROR  {r.error[:90]}"
    mark = "ok   " if r.ok else "WRONG"
    return (
        f"  {r.name:<24} {mark}  customers {r.customers}  delivered x{r.deliveries}  "
        f"inbox {r.inbox}  replies sent {r.sent}  duplicates {r.duplicate_replies}  "
        f"lost {r.lost_replies}"
    )


def format_report(results: Sequence[ScenarioResult]) -> str:
    counted = [r for r in results if not r.known_gap]
    gaps = [r for r in results if r.known_gap]
    dup_replies, dup_inbox = totals(results)
    lines = [
        f"Duplicates: {len(counted)} scenarios in the gate (T16 and chaos)",
        f"  duplicate replies {dup_replies}   duplicate inbox rows {dup_inbox}   (target 0)",
        "",
    ]
    lines.extend(_row(r) for r in counted)
    for r in counted:
        if not r.ok and not r.error:
            lines.append(f"\n  {r.name}: {r.what}")
            for who, t in r.tallies.items():
                if t.duplicate_inbox or t.duplicate_replies or t.lost_replies:
                    lines.append(
                        f"    {who}: messages {t.wamids}, inbox {t.inbox}, queued {t.queued}, "
                        f"sent {t.sent}"
                    )
    if gaps:
        lines.append("\nKnown gap (shown, NOT counted in the gate):")
        for r in gaps:
            lines.append(_row(r))
            lines.append(f"      {r.what}")
            if not r.error and r.duplicate_replies == 0:
                lines.append("      (no duplicate seen this time)")
    verdict = "MET" if gate_met(results) else "NOT MET"
    lines.append(f"\nGate (blueprint 11.4): 0 duplicate replies, T16 and chaos. {verdict}")
    return "\n".join(lines)
