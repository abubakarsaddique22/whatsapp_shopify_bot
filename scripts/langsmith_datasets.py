r"""Upload the evaluation sets to LangSmith as datasets (Phase 12, Step 7).

    uv run python scripts\langsmith_datasets.py --dry-run       count only, nothing is sent
    uv run python scripts\langsmith_datasets.py                 every set
    uv run python scripts\langsmith_datasets.py --set language  one set

Needs LANGSMITH_API_KEY in .env (not for --dry-run). Safe to run again: only new or changed rows
are sent. Every string is masked first (src/app/evals/langsmith_sync.py).
"""

import argparse
import io
import sys

from app.core.errors import ConfigError
from app.evals import langsmith_sync as ls


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload the eval sets as LangSmith datasets")
    parser.add_argument("--set", choices=ls.SETS, help="only this set (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="count the examples, send nothing")
    args = parser.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):  # Urdu text must not crash a Windows console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    names = [args.set] if args.set else list(ls.SETS)
    examples = {name: ls.examples_for(name) for name in names}
    if args.dry_run:
        for name, items in examples.items():
            print(f"{ls.dataset_name(name):<20} {len(items):>4} examples")
        print("dry run: nothing was sent")
        return 0
    try:
        client = ls.make_client()
    except ConfigError as exc:
        print(f"langsmith_datasets: {exc.message}")
        return 1
    for name, items in examples.items():
        report = ls.push_dataset(client, name, items)
        print(
            f"{report.name:<20} added {report.added}, updated {report.updated}, "
            f"unchanged {report.unchanged}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
