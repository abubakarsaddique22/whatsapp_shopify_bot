"""Injection scan hook for text that will be shown to the model (Phase 8 hook, Phase 11 full).

    findings = scan_text(cleaned_policy_text)      # [] when nothing looks wrong
    findings                                       # e.g. ["ignore_instructions"]

Policies and pages are written by the merchant, so they are untrusted text. If one of them
contains sentences that talk TO the model ("ignore all previous instructions", fake <system>
tags), rag/ingest.py does not index it and reports it. The previous version of that document
stays active, so an attack in a new edit cannot replace a good policy.

This is a first, small list of patterns. It only needs to be good enough for the hook. Phase 11
makes the full guardrail (more patterns, Roman Urdu and Urdu script, a test set in
evals/adversarial.jsonl). Every pattern is narrow on purpose: a normal refund policy must never
be flagged.
"""

import re
from collections.abc import Callable

Scanner = Callable[[str], list[str]]

_FLAGS = re.IGNORECASE

_PATTERNS: dict[str, re.Pattern[str]] = {
    # "ignore all previous instructions", "disregard the above rules", "override your prompt"
    "ignore_instructions": re.compile(
        r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}"
        r"\b(?:instructions?|rules?|prompts?)\b",
        _FLAGS,
    ),
    # "reveal your system prompt", "print the hidden instructions"
    "reveal_prompt": re.compile(
        r"\b(?:reveal|show|print|repeat|leak)\b[^.\n]{0,40}"
        r"\b(?:system|hidden|developer)\s+(?:prompt|message|instructions?)\b",
        _FLAGS,
    ),
    # fake role or prompt tags: <system>, </assistant>, <policy_passages>
    "fake_tags": re.compile(
        r"</?\s*(?:system|assistant|developer|tool|policy_passages)\b[^>]*>", _FLAGS
    ),
    # "you are now ..." (a new persona)
    "new_persona": re.compile(r"\byou\s+are\s+now\b", _FLAGS),
    # Roman Urdu: "pichli hidayat bhool jao", "hidayaat ignore karo"
    "roman_urdu_ignore": re.compile(
        r"\bhidaya+t\b[^.\n]{0,30}\b(?:bhool|nazar\s*andaz|ignore)\b",
        _FLAGS,
    ),
}


def scan_text(text: str) -> list[str]:
    """Names of the patterns that match. An empty list means the text looks normal."""
    return [name for name, pattern in _PATTERNS.items() if pattern.search(text)]
