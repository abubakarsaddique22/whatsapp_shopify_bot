"""Phase 12, Step 6: all 25 blueprint test cases (T01 to T25) are present and mapped.

Blueprint Phase 12 card, point 9: "All 25 test cases (T01 to T25) are present and mapped". The map
lives in tests/case_map.py; this file checks it against the real test files (by reading them with
`ast`, nothing is imported or run), and checks that docs/test_cases.md is generated from it.
"""

import ast
import re
from functools import cache
from pathlib import Path

import pytest

from tests.case_map import CASES, render_markdown

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "test_cases.md"
T_NAME = re.compile(r"^test_t(\d\d)_")
ALL_IDS = [f"T{n:02d}" for n in range(1, 26)]


@cache
def functions_in(path: str) -> frozenset[str]:
    """Names of the top-level test functions (sync and async) in one file under the repo root."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef)
    return frozenset(n.name for n in tree.body if isinstance(n, kinds))


def test_exactly_the_25_cases_of_the_blueprint() -> None:
    assert list(CASES) == ALL_IDS


@pytest.mark.parametrize("case_id", ALL_IDS)
def test_every_case_has_a_test_that_really_exists(case_id: str) -> None:
    case = CASES[case_id]
    assert case.tests, f"{case_id} has no test"
    for ref in case.tests:
        path, _, name = ref.partition("::")
        assert (ROOT / path).is_file(), f"{case_id}: {path} does not exist"
        assert name in functions_in(path), f"{case_id}: {name} is not a function in {path}"


def test_every_t_numbered_test_is_in_the_map() -> None:
    """A new test_tNN_... anywhere under tests/ must be listed under TNN (and not elsewhere)."""
    listed = {ref for case in CASES.values() for ref in case.tests}
    missing = []
    for file in sorted((ROOT / "tests").rglob("test_*.py")):
        rel = file.relative_to(ROOT).as_posix()
        for name in functions_in(rel):
            match = T_NAME.match(name)
            if not match:
                continue
            ref = f"{rel}::{name}"
            if ref not in listed:
                missing.append(ref)
            elif ref not in CASES[f"T{match.group(1)}"].tests:
                missing.append(f"{ref} (listed under the wrong case)")
    # one per line, so pytest's short summary cannot cut the list
    assert missing == [], "add these to tests/case_map.py:\n" + "\n".join(missing)


def test_every_case_has_its_types_and_evaluation_sets() -> None:
    for case_id, case in CASES.items():
        assert set(case.types.replace(" ", "").split(",")) <= {"U", "I", "E", "A"}, case_id
        if "E" in case.types:
            assert case.evals, f"{case_id} is an evaluation case but names no evaluation set"
        if "A" in case.types:
            assert any(ref.startswith("tests/adversarial/") for ref in case.tests), case_id


def test_the_docs_page_is_up_to_date() -> None:
    assert DOC.is_file(), "run: uv run python scripts\\test_map.py"
    written = DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
    old = "docs/test_cases.md is old: run uv run python scripts\\test_map.py"
    assert written == render_markdown() + "\n", old
