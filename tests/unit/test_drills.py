"""The written failure drills (src/app/evals/drills.py) and their scoring. No database."""

import ast
from pathlib import Path

from app.evals.drills import (
    BLUEPRINT_FAILURES,
    DRILLS,
    Drill,
    DrillResult,
    format_report,
    gate_met,
    judge,
    judge_all,
)

DRILL_TESTS = Path(__file__).resolve().parents[1] / "drills" / "test_failure_drills.py"


def drill_test_names() -> set[str]:
    """Names of the test functions written in tests/drills/test_failure_drills.py."""
    tree = ast.parse(DRILL_TESTS.read_text(encoding="utf-8"))
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        node.name
        for node in tree.body
        if isinstance(node, functions) and node.name.startswith("test_")
    }


def drill(gap: bool = False) -> Drill:
    return Drill(
        name="shopify_down",
        breaks="x",
        expected="y",
        tests=("test_a", "test_b"),
        gap="not honest yet" if gap else None,
        gap_tests=("test_gap",) if gap else (),
    )


# ---- the written drills


def test_every_blueprint_failure_has_a_drill() -> None:
    for failure in BLUEPRINT_FAILURES:
        assert any(d.name.startswith(failure) for d in DRILLS), failure


def test_every_drill_has_an_expected_behaviour_and_a_test() -> None:
    for d in DRILLS:
        assert d.breaks and d.expected and d.tests, d.name
        assert bool(d.gap) == bool(d.gap_tests), d.name


def test_every_listed_test_exists_and_every_test_is_listed() -> None:
    listed = {t for d in DRILLS for t in (*d.tests, *d.gap_tests)}
    written = drill_test_names()
    assert listed - written == set(), "listed in drills.py but not written"
    assert written - listed == set(), "written but not listed in drills.py"


def test_gap_tests_are_named_as_gaps() -> None:
    for d in DRILLS:
        assert all(t.startswith("test_gap_") for t in d.gap_tests), d.name
        assert not any(t.startswith("test_gap_") for t in d.tests), d.name


# ---- scoring


def test_all_passed_is_pass() -> None:
    assert judge(drill(), {"test_a": "passed", "test_b": "passed"}).status == "PASS"


def test_one_failed_or_missing_is_fail() -> None:
    result = judge(drill(), {"test_a": "passed", "test_b": "failed"})
    assert result.status == "FAIL" and "test_b: failed" in result.problems
    assert judge(drill(), {"test_a": "passed"}).status == "FAIL"  # test_b missing
    assert judge(drill(), {"test_a": "passed", "test_b": "skipped"}).status == "FAIL"


def test_a_known_gap_that_still_fails_is_gap() -> None:
    outcomes = {"test_a": "passed", "test_b": "passed", "test_gap": "xfailed"}
    assert judge(drill(gap=True), outcomes).status == "GAP"


def test_a_gap_test_that_passes_means_the_gap_is_fixed() -> None:
    for outcome in ("passed", "xpassed", "failed"):
        outcomes = {"test_a": "passed", "test_b": "passed", "test_gap": outcome}
        result = judge(drill(gap=True), outcomes)
        assert result.status == "FAIL"
        assert "gap" in result.problems[0]


def test_gate_needs_every_drill_to_pass() -> None:
    passed = DrillResult(drill(), "PASS")
    gap = DrillResult(drill(gap=True), "GAP")
    failed = DrillResult(drill(), "FAIL", ["test_a: failed"])
    assert gate_met([passed, passed])
    assert not gate_met([passed, gap])  # a known gap is not 'safe and honest' yet
    assert not gate_met([passed, failed])
    assert not gate_met([])


def test_report_names_every_drill_and_the_verdict() -> None:
    outcomes = {t: "passed" for d in DRILLS for t in d.tests}
    outcomes.update({t: "xfailed" for d in DRILLS for t in d.gap_tests})
    results = judge_all(outcomes)
    report = format_report(results)
    for d in DRILLS:
        assert d.name in report
    open_gaps = any(d.gap for d in DRILLS)
    assert ("KNOWN GAP" in report) is open_gaps
    assert ("Gate: NOT MET" if open_gaps else "Gate: MET") in report


def test_report_shows_a_gap_and_a_failure() -> None:
    report = format_report(
        [DrillResult(drill(gap=True), "GAP"), DrillResult(drill(), "FAIL", ["test_a: failed"])]
    )
    assert "KNOWN GAP: not honest yet" in report
    assert "PROBLEM: test_a: failed" in report
    assert "Gate: NOT MET" in report
