"""Run the failure drills and print the report (Phase 12, Step 5; Blueprint Phase 12 card).

    uv run python scripts\\failure_drills.py              all five drills, report, exit code
    uv run python scripts\\failure_drills.py --allow-gaps  exit 0 when only known gaps are open

The drills are the tests in tests\\drills (real worker, real Postgres, every test rolled back, no
network, no phone, no model cost). What each drill must show is written in
src\\app\\evals\\drills.py; this script runs the tests and judges every drill against it.
Needs: docker compose up -d db, and uv run alembic upgrade head.

Exit code: 0 = gate met (or only known gaps with --allow-gaps), 1 = a drill failed or a gap is
still open.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.evals.drills import format_report, gate_met, judge_all  # noqa: E402


class OutcomeCollector:
    """A tiny pytest plugin: the outcome of every test, by function name."""

    def __init__(self) -> None:
        self.outcomes: dict[str, str] = {}
        self.database_down = False

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.when != "call" and report.passed:
            return  # setup and teardown only matter when they go wrong
        name = report.nodeid.split("::")[-1].split("[")[0]
        outcome = str(report.outcome)
        if hasattr(report, "wasxfail"):
            outcome = "xfailed" if report.skipped else "xpassed"
        elif report.when == "setup" and report.failed:
            outcome = "error in setup"  # the test never ran (database down, fixture broken)
            if "ConnectionRefusedError" in str(report.longrepr):
                self.database_down = True
        if self.outcomes.get(name) == "failed":
            return  # a failed call stays failed, whatever the teardown says
        self.outcomes[name] = outcome


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Phase 12 failure drills.")
    parser.add_argument(
        "--allow-gaps", action="store_true", help="exit 0 when only known gaps are open"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show every test")
    args = parser.parse_args()

    os.chdir(ROOT)
    collector = OutcomeCollector()
    pytest_args = ["tests/drills", "-p", "no:cacheprovider", "-v" if args.verbose else "-q"]
    pytest.main(pytest_args, plugins=[collector])

    if not collector.outcomes:
        print(
            "\nNo drill test ran. Is Postgres up (docker compose up -d db) and "
            "`uv run alembic upgrade head` done?"
        )
        return 1

    results = judge_all(collector.outcomes)
    print(format_report(results))
    if collector.database_down:
        print(
            "\nThe drills could not reach Postgres (connection refused): this is not a bot "
            "failure. Start it with `docker compose up -d db`, then run this script again."
        )
    if any(r.status == "FAIL" for r in results):
        return 1
    if gate_met(results) or args.allow_gaps:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
