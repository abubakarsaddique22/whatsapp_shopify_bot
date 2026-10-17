"""Sync worker: applies the pending Shopify events to the catalogue mirror (Phase 10, step 4).

Run it in its own window (next to the web server and the bot worker):

    uv run python -m app.sync.worker

Each pass takes ONE pending event (sync/handlers.py), refetches the object from Shopify and updates
the mirror. A failing event is retried later with a growing wait and becomes 'failed' (dead
letter) after MAX_ATTEMPTS tries. It is separate from app.worker, so a slow Shopify call can never
delay a customer's WhatsApp reply.
"""

import asyncio
import logging

from app.core.logging import setup_logging
from app.core.settings import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.sync.handlers import process_one

log = logging.getLogger(__name__)

POLL_SECONDS = 2  # wait when there is nothing to do


async def main() -> None:
    setup_logging(get_settings().log_level)
    sessionmaker = get_sessionmaker()
    log.info("sync_worker_started")
    try:
        while True:
            try:
                processed = await process_one(sessionmaker)
            except Exception:  # for example the database is down: wait and try again
                log.exception("sync_worker_loop_error")
                await asyncio.sleep(5)
                continue
            if not processed:
                await asyncio.sleep(POLL_SECONDS)
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("sync_worker_stopped")
