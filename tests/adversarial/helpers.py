"""Shared helpers of the adversarial tests: the rows of evals/adversarial.jsonl."""

import json
from pathlib import Path
from typing import Any

ADVERSARIAL = Path(__file__).resolve().parents[2] / "evals" / "adversarial.jsonl"


def load_rows() -> list[dict[str, Any]]:
    lines = ADVERSARIAL.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def customer_rows(first_intent: str) -> list[dict[str, Any]]:
    """Customer messages whose FIRST accepted router intent is `first_intent`."""
    return [r for r in load_rows() if (r.get("ok_intents") or [None])[0] == first_intent]


def document_rows(must_flag: bool) -> list[dict[str, Any]]:
    """Store texts (policy, page, product description) that must / must not be flagged."""
    return [r for r in load_rows() if r.get("must_flag") is must_flag]


def ids(rows: list[dict[str, Any]]) -> list[str]:
    return [r["id"] for r in rows]
