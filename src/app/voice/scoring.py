"""Phase 6, step 5b: the arithmetic of the speech bake-off (Blueprint 6.6).

No network, no audio and no database here: only text and numbers, so every function is tested
without a speech provider (tests/unit/voice/test_scoring.py). scripts/bakeoff.py does the
work that needs audio and providers, then hands the results to this module.

    result = build_result(provider="groq:whisper-large-v3", file="ur-001.ogg",
                          clip_class="urdu", reference="...", duration_s=4.2,
                          hypothesis="...", latency_ms=800)
    print(format_report(summarize([result, ...])))

What is measured (blueprint 6.6, point 4 and 5):
- WER and CER after normalisation: lower case, no punctuation, no diacritics, Arabic letter
  variants unified (ي and ى become ی, ك becomes ک), Arabic-Indic digits become 0 to 9.
- The output script (arabic, gurmukhi, devanagari, latin, mixed). A Punjabi clip that comes
  back in Gurmukhi is a different problem from one that comes back in Urdu script.
- Latency p50 and p95, failure rate, and how many transcripts pass the confidence gate.
- Intent accuracy when the router was run on the transcript (a transcript can have a high WER
  and still give the right intent, or the reverse).

IMPORTANT: the reference text must be written in the SAME script the provider returns. A
reference in Roman Urdu against a transcript in Urdu script gives a WER near 100% for the
wrong reason. Decide the script per class before labelling the clips.
"""

import math
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

_CHAR_MAP = str.maketrans(
    {
        **{chr(0x0660 + i): str(i) for i in range(10)},  # Arabic-Indic digits
        **{chr(0x06F0 + i): str(i) for i in range(10)},  # Persian/Urdu digits
        "ي": "ی",  # Arabic yeh -> Farsi yeh
        "ى": "ی",  # alef maksura -> Farsi yeh
        "ك": "ک",  # Arabic kaf -> Keheh
        "ـ": None,  # tatweel (stretching line)
    }
)

_SCRIPT_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "arabic": ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)),
    "gurmukhi": ((0x0A00, 0x0A7F),),
    "devanagari": ((0x0900, 0x097F),),
    "latin": ((0x0041, 0x005A), (0x0061, 0x007A), (0x00C0, 0x024F)),
}


def normalize_text(text: str) -> str:
    """The text as it is compared: no case, no punctuation, no diacritics, one space."""
    text = unicodedata.normalize("NFKC", text or "").translate(_CHAR_MAP).lower()
    kept: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if ch.isspace():
            kept.append(" ")
        elif category in ("Mn", "Cf", "Cc") or ch in "'’":
            continue  # marks, invisible characters, apostrophes inside words
        elif category[0] in "PS":
            kept.append(" ")
        else:
            kept.append(ch)
    return " ".join("".join(kept).split())


def edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
    """Levenshtein distance between two sequences (words or characters)."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, start=1):
        current = [i]
        for j, y in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def wer(reference: str, hypothesis: str) -> float | None:
    """Word error rate as a ratio (0.25 = 25%). None when the reference has no words."""
    ref = normalize_text(reference).split()
    if not ref:
        return None
    return edit_distance(ref, normalize_text(hypothesis).split()) / len(ref)


def cer(reference: str, hypothesis: str) -> float | None:
    """Character error rate as a ratio. Spaces count as characters. None for an empty reference."""
    ref = list(normalize_text(reference))
    if not ref:
        return None
    return edit_distance(ref, list(normalize_text(hypothesis))) / len(ref)


def script_of(text: str) -> str:
    """arabic | gurmukhi | devanagari | latin | mixed (no script above 50%) | none (no letters)."""
    counts: Counter[str] = Counter()
    for ch in text or "":
        if not ch.isalpha():
            continue
        code = ord(ch)
        for name, ranges in _SCRIPT_RANGES.items():
            if any(low <= code <= high for low, high in ranges):
                counts[name] += 1
                break
    total = sum(counts.values())
    if total == 0:
        return "none"
    name, count = counts.most_common(1)[0]
    return name if count / total > 0.5 else "mixed"


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile (p from 0 to 100). None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


class ClipResult(BaseModel):
    """One clip, one provider. Contains real transcripts: keep result files out of Git."""

    provider: str  # label such as "groq:whisper-large-v3"
    file: str
    clip_class: str  # urdu | punjabi | english | mixed
    duration_s: float = 0.0
    reference: str = ""
    hypothesis: str = ""
    wer: float | None = None
    cer: float | None = None
    script: str = "none"
    language: str | None = None  # what the provider says it heard
    confidence: float | None = None
    latency_ms: int | None = None
    gate_reason: str | None = None  # None = the confidence gate let it through
    error: str | None = None  # set when the clip could not be transcribed at all
    intent_expected: str | None = None
    intent_got: str | None = None  # None = the router was not run


def build_result(
    *,
    provider: str,
    file: str,
    clip_class: str,
    reference: str,
    duration_s: float = 0.0,
    hypothesis: str = "",
    language: str | None = None,
    confidence: float | None = None,
    latency_ms: int | None = None,
    gate_reason: str | None = None,
    error: str | None = None,
    intent_expected: str | None = None,
    intent_got: str | None = None,
) -> ClipResult:
    """Fill in WER, CER and script. A failed clip (error set) gets no scores."""
    scored = error is None
    return ClipResult(
        provider=provider,
        file=file,
        clip_class=clip_class,
        duration_s=duration_s,
        reference=reference,
        hypothesis=hypothesis,
        wer=wer(reference, hypothesis) if scored else None,
        cer=cer(reference, hypothesis) if scored else None,
        script=script_of(hypothesis) if scored else "none",
        language=language,
        confidence=confidence,
        latency_ms=latency_ms,
        gate_reason=gate_reason,
        error=error,
        intent_expected=intent_expected,
        intent_got=intent_got,
    )


def _pct(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 1) if denominator else None


def _mean_pct(values: list[float]) -> float | None:
    return round(100 * sum(values) / len(values), 1) if values else None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value)


def _metrics(rows: list[ClipResult]) -> dict[str, Any]:
    ok = [r for r in rows if r.error is None]
    latencies = [float(r.latency_ms) for r in ok if r.latency_ms is not None]
    routed = [r for r in ok if r.intent_expected and r.intent_got is not None]
    spoken = [r for r in ok if r.hypothesis.strip()]
    scripts = Counter(r.script for r in spoken)
    return {
        "clips": len(rows),
        "failures": len(rows) - len(ok),
        "failure_pct": _pct(len(rows) - len(ok), len(rows)),
        "wer_pct": _mean_pct([r.wer for r in ok if r.wer is not None]),
        "cer_pct": _mean_pct([r.cer for r in ok if r.cer is not None]),
        "latency_p50_ms": _round(percentile(latencies, 50)),
        "latency_p95_ms": _round(percentile(latencies, 95)),
        "gate_pass_pct": _pct(sum(r.gate_reason is None for r in ok), len(ok)),
        "intent_pct": _pct(sum(r.intent_got == r.intent_expected for r in routed), len(routed)),
        "scripts": {name: _pct(count, len(spoken)) for name, count in sorted(scripts.items())},
        "audio_minutes": round(sum(r.duration_s for r in rows) / 60, 2),
    }


def summarize(results: list[ClipResult]) -> dict[str, dict[str, dict[str, Any]]]:
    """{provider: {class: metrics, ..., "all": metrics}}. The "all" row comes last."""
    by_provider: defaultdict[str, list[ClipResult]] = defaultdict(list)
    for result in results:
        by_provider[result.provider].append(result)

    summary: dict[str, dict[str, dict[str, Any]]] = {}
    for provider, rows in sorted(by_provider.items()):
        by_class: defaultdict[str, list[ClipResult]] = defaultdict(list)
        for row in rows:
            by_class[row.clip_class].append(row)
        groups = {name: _metrics(group) for name, group in sorted(by_class.items())}
        groups["all"] = _metrics(rows)
        summary[provider] = groups
    return summary


def _cell(value: object) -> str:
    return "n/a" if value is None else str(value)


def format_report(
    summary: dict[str, dict[str, dict[str, Any]]], *, min_clips_per_class: int = 100
) -> str:
    """A plain-text table per provider. Numbers only: no claim that a provider is 'good'."""
    lines: list[str] = []
    small: set[str] = set()
    for provider, groups in summary.items():
        lines += [
            f"== {provider}",
            f"{'class':<10}{'clips':>6}{'fail%':>7}{'WER%':>7}{'CER%':>7}"
            f"{'p50ms':>7}{'p95ms':>7}{'gate%':>7}{'intent%':>8}  scripts",
        ]
        for name, m in groups.items():
            scripts = ", ".join(f"{k} {v}%" for k, v in m["scripts"].items()) or "n/a"
            lines.append(
                f"{name:<10}{m['clips']:>6}{_cell(m['failure_pct']):>7}{_cell(m['wer_pct']):>7}"
                f"{_cell(m['cer_pct']):>7}{_cell(m['latency_p50_ms']):>7}"
                f"{_cell(m['latency_p95_ms']):>7}{_cell(m['gate_pass_pct']):>7}"
                f"{_cell(m['intent_pct']):>8}  {scripts}"
            )
            if name != "all" and m["clips"] < min_clips_per_class:
                small.add(name)
        lines.append(f"audio: {groups['all']['audio_minutes']} minutes (for the price per minute)")
        lines.append("")
    if small:
        lines.append(
            f"NOTE: class(es) {', '.join(sorted(small))} have fewer than {min_clips_per_class} "
            "clips. Blueprint 6.6 asks for at least 100 per class: below that this is a "
            "smoke test, not a baseline. Do not quote these numbers to the client."
        )
    return "\n".join(lines).rstrip()
