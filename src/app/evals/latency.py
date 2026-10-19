"""Latency evaluation (Phase 12, Step 4f; Blueprint 11.4: text p50 <= 8 s and p95 <= 15 s).

What is timed: from the moment the customer's message is stored in `whatsapp_inbox` (the webhook
is done, it answers in milliseconds) to the moment the reply rows are committed to
`whatsapp_outbox`. That is "webhook to outbox" of blueprint 11.4. It runs the REAL worker code
(`worker.handle_inbox`) with the app's real models and the real dev store, one fresh customer per
question. Sending to Meta and the network are NOT in the number.

One addition, on purpose: the real worker sleeps up to POLL_SECONDS (2 s) when its queue is empty,
so a message can wait that long before the worker even looks at it. The gate uses the measured
time PLUS that worst-case wait (`poll_allowance`), so the number is not better than production.
The report prints both.

Voice is timed separately (blueprint 11.4). It needs real voice clips (evals/voice_manifest.csv,
Phase 6 bake-off), so this module only knows the kind `voice` and prints "no timings yet" while
there are none. Voice has no gate number.

This module only scores. Running the pipeline is `pipeline.measure_text` (scripts/run_eval.py
--set latency). No model and no database are used here.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

TEXT_P50_TARGET = 8.0  # seconds, blueprint 11.4 (proposed)
TEXT_P95_TARGET = 15.0
MIN_SAMPLES = 20  # fewer good runs than this: a p95 means nothing (my choice, not the blueprint's)
POLL_ALLOWANCE = 2.0  # seconds: worker.POLL_SECONDS, the worst-case wait before a pass starts
VOICE_MANIFEST = Path("evals") / "voice_manifest.csv"
NOTE_CLIP = 70


@dataclass
class LatencyRun:
    id: str
    kind: str  # text | voice
    seconds: float
    ok: bool = True  # False: provider error, crash, no reply. Not counted in the percentiles.
    note: str = ""  # why a run is not ok


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least `pct` % of the values at or
    below it. With 20 values p95 is the 19th smallest. None when there are no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def voice_clip_count(path: Path = VOICE_MANIFEST) -> int:
    """Clips listed in the voice manifest (the first line is the header)."""
    if not path.exists():
        return 0
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return max(0, len(lines) - 1)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} s"


def summarize(runs: Sequence[LatencyRun], poll_allowance: float = POLL_ALLOWANCE) -> dict:
    out: dict = {"poll_allowance": poll_allowance}
    for kind in ("text", "voice"):
        group = [r for r in runs if r.kind == kind]
        good = [r.seconds for r in group if r.ok]
        p50, p95 = percentile(good, 50), percentile(good, 95)
        out[kind] = {
            "runs": len(group),
            "good": len(good),
            "bad": [r for r in group if not r.ok],
            "p50": p50,
            "p95": p95,
            "max": max(good) if good else None,
            # what the gate compares: the measured time plus the worst-case poll wait
            "p50_total": None if p50 is None else p50 + poll_allowance,
            "p95_total": None if p95 is None else p95 + poll_allowance,
        }
    return out


def gate_met(runs: Sequence[LatencyRun], poll_allowance: float = POLL_ALLOWANCE) -> bool:
    """Text only: enough good runs, and p50 and p95 (each plus the poll wait) inside the targets."""
    text = summarize(runs, poll_allowance)["text"]
    if text["good"] < MIN_SAMPLES:
        return False
    return text["p50_total"] <= TEXT_P50_TARGET and text["p95_total"] <= TEXT_P95_TARGET


def format_report(
    runs: Sequence[LatencyRun], *, voice_clips: int = 0, poll_allowance: float = POLL_ALLOWANCE
) -> str:
    s = summarize(runs, poll_allowance)
    text = s["text"]
    lines = [
        f"Latency, webhook to outbox: text {text['good']} good runs of {text['runs']}",
        f"  measured      p50 {_fmt(text['p50'])}   p95 {_fmt(text['p95'])}   "
        f"max {_fmt(text['max'])}",
        f"  + poll wait   p50 {_fmt(text['p50_total'])}   p95 {_fmt(text['p95_total'])}   "
        f"(worst case, the worker sleeps up to {poll_allowance:.0f} s when idle)",
        f"  targets       p50 <= {TEXT_P50_TARGET:.0f} s   p95 <= {TEXT_P95_TARGET:.0f} s",
    ]
    if text["good"] < MIN_SAMPLES:
        lines.append(
            f"  NOT ENOUGH GOOD RUNS: {MIN_SAMPLES} needed for a p95 "
            f"(run again, or raise --limit)"
        )
    if text["bad"]:
        lines.append(f"\nRuns not counted ({len(text['bad'])}):")
        for r in text["bad"]:
            lines.append(f"  {r.id}: {r.note[:NOTE_CLIP]}")
    voice = s["voice"]
    if voice["runs"]:
        lines.append(
            f"\nVoice (no gate, measured separately): {voice['good']} good runs, "
            f"p50 {_fmt(voice['p50'])}, p95 {_fmt(voice['p95'])}"
        )
    else:
        lines.append(
            f"\nVoice: no timings yet ({voice_clips} clips in {VOICE_MANIFEST}). Time it with the "
            "Phase 6 baseline clips; it has no gate number."
        )
    verdict = "MET" if gate_met(runs, poll_allowance) else "NOT MET"
    lines.append(
        f"\nGate (blueprint 11.4, proposed): text p50 {TEXT_P50_TARGET:.0f} s and "
        f"p95 {TEXT_P95_TARGET:.0f} s. {verdict}"
    )
    return "\n".join(lines)
