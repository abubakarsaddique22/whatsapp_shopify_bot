"""Tests for the product Q&A evaluation helpers (Phase 12, Step 3). No bot, no network."""

import json
from pathlib import Path

from app.evals.product_qa import (
    DATA,
    format_report,
    load_rows,
    score,
    select,
    tally,
    vocabulary,
)

ROWS = [
    {
        "id": "price-01", "kind": "price", "lang": "en", "question": "How much is the Box?",
        "expected": {"amount": "3000.00", "currency": "PKR"},
    },
    {
        "id": "stock-01", "kind": "stock", "lang": "roman_ur", "question": "Box available hai?",
        "expected": {"stock": "in_stock"},
    },
    {
        "id": "colors-01", "kind": "colors", "lang": "mixed", "question": "Watch A colours?",
        "expected": {"values": ["Black", "Blue"]},
    },
    {
        "id": "colors-02", "kind": "colors", "lang": "en", "question": "Watch B colours?",
        "expected": {"values": ["Green", "Red"]},
    },
]


def test_load_rows_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "qa.jsonl"
    path.write_text(json.dumps(ROWS[0]) + "\n\n" + json.dumps(ROWS[1]) + "\n", encoding="utf-8")
    assert [r["id"] for r in load_rows(path)] == ["price-01", "stock-01"]


def test_the_real_file_is_well_formed() -> None:
    rows = load_rows(DATA)
    assert len(rows) >= 60  # blueprint 11.3: at least 60 questions
    assert {r["kind"] for r in rows} <= {"price", "stock", "colors", "sizes"}
    assert len({r["id"] for r in rows}) == len(rows)
    for row in rows:
        assert row["question"].strip()
        assert row["expected"]


def test_select_kind_and_limit() -> None:
    assert [r["id"] for r in select(ROWS, "colors", None)] == ["colors-01", "colors-02"]
    assert [r["id"] for r in select(ROWS, None, 2)] == ["price-01", "stock-01"]
    assert len(select(ROWS, None, None)) == 4


def test_vocabulary_collects_colours_of_all_products() -> None:
    assert vocabulary(ROWS) == {"colors": {"Black", "Blue", "Green", "Red"}}


def test_score_uses_the_vocabulary_to_catch_a_wrong_colour() -> None:
    vocab = vocabulary(ROWS)
    assert score(ROWS[2], "Black and Blue.", vocab).ok
    wrong = score(ROWS[2], "Black, Blue and Red.", vocab)
    assert not wrong.ok
    assert "Red" in wrong.reason


def test_tally_by_kind_and_language() -> None:
    vocab = vocabulary(ROWS)
    results = [
        score(ROWS[0], "Rs 3,000", vocab),
        score(ROWS[1], "Currently out of stock.", vocab),
        score(ROWS[2], "Black and Blue", vocab),
    ]
    assert tally(results, "kind") == {"colors": (1, 1), "price": (1, 1), "stock": (0, 1)}
    assert tally(results, "lang")["roman_ur"] == (0, 1)


def test_report_lists_the_wrong_answers_and_the_gate() -> None:
    vocab = vocabulary(ROWS)
    good = [score(ROWS[0], "Rs 3,000", vocab)]
    assert "NOT MET" not in format_report(good)
    assert "MET" in format_report(good)
    bad = good + [score(ROWS[1], "Currently out of stock.", vocab)]
    report = format_report(bad)
    assert "1/2 passed (50.0%)" in report
    assert "stock-01" in report
    assert "NOT MET" in report
