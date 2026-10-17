"""Reconciliation: bring the catalogue mirror and the knowledge base back in line with Shopify
(Phase 10, step 6; Blueprint 4.7).

Webhooks are fast but can be missed (server down, tunnel down, Shopify gave up). Three jobs repair
that. Each run is written to sync_runs (when it started and ended, what it changed, the error).

    delta   every few minutes: products Shopify says were edited since the last good run are
            refetched and the mirror is updated. A missed webhook is repaired here. A product
            DELETED in Shopify is not listed by Shopify at all, so delta cannot see it.
    full    nightly: every visible product is compared with the mirror (add, update, hide).
            This is where a deleted product is found. It also catches stock changes that did
            not touch the product's edit time.
    policy  hourly: policies and pages go through the knowledge-base ingest (rag/ingest.py).
            Shopify has no webhook for them. Only changed documents are embedded again.

Run one job:   uv run python -m app.jobs.reconcile --mode delta
Run the loop:  uv run python -m app.jobs.reconcile --loop     (all three, at their own intervals)
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalog.importer import sync_mirror, sync_product
from app.core.errors import AppError
from app.core.ids import correlation_id_var, new_correlation_id
from app.core.settings import get_settings
from app.db import repositories as repo
from app.domain.catalog import Product
from app.integrations.embeddings.factory import get_embedder
from app.integrations.shopify import gateway
from app.rag.ingest import fetch_sources, ingest_documents

log = logging.getLogger(__name__)

OVERLAP = timedelta(minutes=2)  # delta starts a little before the last watermark: no gap at all
FIRST_LOOKBACK = timedelta(days=1)  # the very first delta run looks this far back
RETRY_AFTER_ERROR_SECONDS = 60  # the loop waits this long if a run could not even be recorded

# Kinds whose runs carry a watermark (the start time of the run, saved when it finished 'ok').
WATERMARK_KINDS: frozenset[str] = frozenset({"delta"})

Job = Callable[[async_sessionmaker[AsyncSession], datetime], Awaitable[dict[str, Any]]]


class JobResult(BaseModel):
    kind: str
    run_id: int
    status: str  # ok | failed
    counts: dict[str, Any]
    error: str | None = None


def delta_since(watermark: datetime | None, started_at: datetime) -> datetime:
    """From when the delta job asks Shopify for changes."""
    if watermark is None:
        return started_at - FIRST_LOOKBACK
    return watermark - OVERLAP


# ---- the work of each job


async def apply_delta(session: AsyncSession, changed: dict[str, Product | None]) -> dict[str, int]:
    """Bring the mirror in line with the products Shopify listed as changed.

    A product that maps to None is not customer-visible any more and gets hidden. The caller
    owns the transaction.
    """
    counts = {"seen": len(changed), "added": 0, "updated": 0, "unchanged": 0, "hidden": 0}
    for product_id, product in changed.items():
        counts[await sync_product(session, product_id, product)] += 1
    return counts


async def _delta_job(sessionmaker: async_sessionmaker[AsyncSession], since: datetime) -> dict:
    changed = await gateway.list_products_changed_since(since)  # no database held while waiting
    async with sessionmaker() as session, session.begin():
        return await apply_delta(session, changed)


async def _full_job(sessionmaker: async_sessionmaker[AsyncSession], since: datetime) -> dict:
    products = await gateway.list_products()
    async with sessionmaker() as session, session.begin():
        report = await sync_mirror(session, products)
    return asdict(report)


async def _policy_job(sessionmaker: async_sessionmaker[AsyncSession], since: datetime) -> dict:
    docs = await fetch_sources()
    embedder = get_embedder()
    async with sessionmaker() as session, session.begin():
        report = await ingest_documents(session, embedder, docs)
    return report.model_dump(mode="json")


JOBS: dict[str, Job] = {"delta": _delta_job, "full": _full_job, "policy": _policy_job}


# ---- one recorded run


async def run_job(sessionmaker: async_sessionmaker[AsyncSession], kind: str) -> JobResult:
    """Run one job and write it to sync_runs (running, then ok or failed).

    The 'running' row is committed first. A failed run keeps no watermark, so the next delta
    run starts from the last GOOD run and nothing is skipped.
    """
    correlation_id_var.set(new_correlation_id())
    async with sessionmaker() as session, session.begin():
        run_id, started_at = await repo.start_sync_run(session, kind)
        previous = await repo.last_ok_watermark(session, kind) if kind in WATERMARK_KINDS else None
    since = delta_since(previous, started_at)
    log.info("reconcile_started", extra={"kind": kind, "run_id": run_id})

    try:
        counts = await JOBS[kind](sessionmaker, since)
    except Exception as exc:
        error = exc.message if isinstance(exc, AppError) else type(exc).__name__
        log.exception("reconcile_failed", extra={"kind": kind, "run_id": run_id})
        async with sessionmaker() as session, session.begin():
            await repo.finish_sync_run(session, run_id, "failed", {}, error, None)
        return JobResult(kind=kind, run_id=run_id, status="failed", counts={}, error=error)

    watermark = started_at if kind in WATERMARK_KINDS else None
    async with sessionmaker() as session, session.begin():
        await repo.finish_sync_run(session, run_id, "ok", counts, None, watermark)
    log.info("reconcile_done", extra={"kind": kind, "run_id": run_id, "counts": counts})
    return JobResult(kind=kind, run_id=run_id, status="ok", counts=counts)


# ---- the loop (development; in production a scheduler runs one mode at a time)


def wait_before_next(status: str, interval: float) -> float:
    """Seconds until a job runs again. A failed run (Shopify down ...) is tried again soon, not
    after a whole interval: a failed nightly 'full' must not wait another 24 hours."""
    if status == "ok":
        return interval
    return min(float(RETRY_AFTER_ERROR_SECONDS), interval)


async def run_loop(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Run all three jobs forever, each at its own interval. All three run once at the start."""
    settings = get_settings()
    every = {
        "full": float(max(1, settings.reconcile_full_hours) * 3600),
        "policy": float(max(1, settings.reconcile_policy_minutes) * 60),
        "delta": float(max(1, settings.reconcile_delta_minutes) * 60),
    }
    due = dict.fromkeys(every, 0.0)
    while True:
        ran = False
        for kind in ("full", "policy", "delta"):
            if time.monotonic() < due[kind]:
                continue
            ran = True
            try:
                result = await run_job(sessionmaker, kind)
                due[kind] = time.monotonic() + wait_before_next(result.status, every[kind])
            except Exception:  # for example the database is down: try this job again soon
                log.exception("reconcile_loop_error", extra={"kind": kind})
                due[kind] = time.monotonic() + RETRY_AFTER_ERROR_SECONDS
        if not ran:
            await asyncio.sleep(5)
