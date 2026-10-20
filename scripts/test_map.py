"""Write docs/test_cases.md from tests/case_map.py (Phase 12, Step 6).

    uv run python scripts\\test_map.py           write the page
    uv run python scripts\\test_map.py --check   only check the page is up to date (exit 1 if not)

Run it after you change tests/case_map.py. tests/unit/test_case_map.py fails while the page is old.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.case_map import CASES, render_markdown  # noqa: E402

DOC = ROOT / "docs" / "test_cases.md"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate docs/test_cases.md")
    parser.add_argument("--check", action="store_true", help="only check, do not write")
    args = parser.parse_args()

    page = render_markdown() + "\n"
    if args.check:
        current = DOC.read_text(encoding="utf-8").replace("\r\n", "\n") if DOC.is_file() else ""
        if current != page:
            print("docs/test_cases.md is out of date: run uv run python scripts\\test_map.py")
            return 1
        print("docs/test_cases.md is up to date")
        return 0
    DOC.write_text(page, encoding="utf-8", newline="\n")
    open_cases = [case_id for case_id, case in CASES.items() if case.open]
    listed = ", ".join(open_cases) or "-"
    print(f"wrote {DOC.relative_to(ROOT)}: {len(CASES)} cases, still open: {listed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
