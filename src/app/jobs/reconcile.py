"""Reconciliation job entry point (Phase 10, step 6). Details: app/sync/reconcile.py.

    uv run python -m app.jobs.reconcile --mode delta     products edited since the last good run
    uv run python -m app.jobs.reconcile --mode full      every product against the mirror (nightly)
    uv run python -m app.jobs.reconcile --mode policy    policies and pages into the knowledge base
    uv run python -m app.jobs.reconcile --loop           all three, each at its own interval

A single --mode run prints what it did and exits with 1 if the run failed.
"""

import argparse
import asyncio
import logging

from app.core.errors import AppError
from app.core.logging import setup_logging
from app.core.settings import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.sync import reconcile

log = logging.getLogger(__name__)


async def main(mode: str | None, loop: bool) -> int:
    setup_logging(get_settings().log_level)
    sessionmaker = get_sessionmaker()
    try:
        if loop:
            log.info("reconcile_loop_started")
            await reconcile.run_loop(sessionmaker)
            return 0
        assert mode is not None
        result = await reconcile.run_job(sessionmaker, mode)
    except AppError as exc:
        print(f"ERROR: {exc.message}")
        return 1
    finally:
        await get_engine().dispose()
    print(f"{result.kind} run #{result.run_id}: {result.status}")
    for key, value in result.counts.items():
        print(f"  {key}: {value}")
    if result.error:
        print(f"  error: {result.error}")
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reconcile the mirror and the knowledge base.")
    parser.add_argument("--mode", choices=sorted(reconcile.JOBS), help="run one job and exit")
    parser.add_argument("--loop", action="store_true", help="run all jobs forever")
    args = parser.parse_args()
    if args.loop == bool(args.mode):
        parser.error("give exactly one of --mode and --loop")
    try:
        raise SystemExit(asyncio.run(main(args.mode, args.loop)))
    except KeyboardInterrupt:
        log.info("reconcile_stopped")
