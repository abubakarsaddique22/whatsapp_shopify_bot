"""Sync monitor: is the sync healthy? (Phase 10, step 7; Blueprint 4.7)

Looks at sync_events (webhook events) and sync_runs (delta / full / policy) and raises ALARMS:

    dead_letter   events that failed for good (5 tries): they are kept apart, status 'failed'
    backlog       the oldest event that is due has waited too long: is the sync worker running?
    run_failed    the newest run of a job ended 'failed'
    run_stuck     the newest run of a job is 'running' for far too long (the process died)
    run_stale     the last good run of a job is older than its interval allows
    no_good_run   a job has never finished ok

    uv run python -m app.sync.monitor                       report; exit code 1 if any alarm
    uv run python -m app.sync.monitor --list-failed         show the dead letter
    uv run python -m app.sync.monitor --retry-failed        put ALL dead-letter events back
    uv run python -m app.sync.monitor --retry-failed --event-id 42     put one back

Every alarm is also written to the log as `sync_alarm` (level ERROR) with a `code`, so a log
alarm (CloudWatch metric filter in Phase 13) can watch for it. Run it from a scheduler every few
minutes. The checks are plain functions (`evaluate`), so they are tested without a database.
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.logging import setup_logging
from app.core.settings import Settings, get_settings
from app.db import repositories as repo
from app.db.session import get_engine, get_sessionmaker

log = logging.getLogger(__name__)

KINDS: tuple[str, ...] = ("delta", "full", "policy")

BACKLOG_MINUTES = 10  # an event that is due should be taken within seconds; 10 min = worker is down
STUCK_RUN_MINUTES = 30  # a run that says 'running' for longer than this has died
# A job is "late" when its last good run is older than interval x factor. The nightly job gets a
# smaller factor because its interval is already long.
STALE_FACTOR: dict[str, float] = {"delta": 3.0, "policy": 3.0, "full": 1.5}


@dataclass(frozen=True)
class Alarm:
    code: str
    message: str


@dataclass(frozen=True)
class RunInfo:
    last_status: str | None = None  # running | ok | failed (newest run)
    last_started: datetime | None = None
    last_ok_finished: datetime | None = None  # when the newest GOOD run finished
    last_error: str | None = None


@dataclass
class Health:
    event_counts: dict[str, int] = field(default_factory=dict)
    oldest_due: datetime | None = None  # received_at of the oldest event that is due now
    runs: dict[str, RunInfo] = field(default_factory=dict)


def intervals(settings: Settings) -> dict[str, timedelta]:
    """How often each job should run (same minimums as the loop in sync/reconcile.py)."""
    return {
        "delta": timedelta(minutes=max(1, settings.reconcile_delta_minutes)),
        "full": timedelta(hours=max(1, settings.reconcile_full_hours)),
        "policy": timedelta(minutes=max(1, settings.reconcile_policy_minutes)),
    }


# ---- the checks (pure: no database, no clock)


def evaluate(health: Health, now: datetime, every: dict[str, timedelta]) -> list[Alarm]:
    """Turn what the database says into a list of alarms. Empty list = healthy."""
    alarms: list[Alarm] = []

    dead = health.event_counts.get("failed", 0)
    if dead:
        alarms.append(
            Alarm(
                "dead_letter",
                f"{dead} event(s) failed for good. Look: --list-failed, then --retry-failed",
            )
        )

    if health.oldest_due is not None:
        waited = now - health.oldest_due
        if waited > timedelta(minutes=BACKLOG_MINUTES):
            alarms.append(
                Alarm(
                    "backlog",
                    f"the oldest waiting event is {_age(waited)} old: is the sync worker running?",
                )
            )

    for kind, interval in every.items():
        run = health.runs.get(kind)
        if run is None or run.last_started is None:
            alarms.append(Alarm("no_good_run", f"{kind}: this job has never run"))
            continue
        if run.last_status == "failed":
            alarms.append(Alarm("run_failed", f"{kind}: the last run failed ({run.last_error})"))
        elif run.last_status == "running" and now - run.last_started > timedelta(
            minutes=STUCK_RUN_MINUTES
        ):
            alarms.append(
                Alarm(
                    "run_stuck",
                    f"{kind}: a run has been 'running' for {_age(now - run.last_started)}"
                    " (the process probably died)",
                )
            )
        if run.last_ok_finished is None:
            alarms.append(Alarm("no_good_run", f"{kind}: no run has finished ok yet"))
        elif now - run.last_ok_finished > interval * STALE_FACTOR.get(kind, 3.0):
            alarms.append(
                Alarm(
                    "run_stale",
                    f"{kind}: the last good run was {_age(now - run.last_ok_finished)} ago"
                    f" (it should run every {_age(interval)})",
                )
            )
    return alarms


def _age(delta: timedelta) -> str:
    """'45 s', '12 min', '3 h 5 min', '2 d 4 h'."""
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


# ---- reading the database


async def collect(session: AsyncSession) -> Health:
    """Read the numbers `evaluate` needs. Read only."""
    health = Health(
        event_counts=await repo.sync_event_status_counts(session),
        oldest_due=await repo.oldest_due_event_time(session),
    )
    for kind in KINDS:
        last = await repo.last_sync_run(session, kind)
        if last is None:
            continue
        health.runs[kind] = RunInfo(
            last_status=last.status,
            last_started=last.started_at,
            last_ok_finished=await repo.last_ok_run_finished(session, kind),
            last_error=last.error,
        )
    return health


def format_report(health: Health, alarms: list[Alarm], now: datetime) -> str:
    counts = health.event_counts
    lines = [
        "Webhook events: "
        + (", ".join(f"{status} {n}" for status, n in sorted(counts.items())) or "none yet"),
    ]
    if health.oldest_due is not None:
        lines.append(f"Oldest event waiting: {_age(now - health.oldest_due)}")
    for kind in KINDS:
        run = health.runs.get(kind)
        if run is None or run.last_started is None:
            lines.append(f"{kind:<7} never ran")
            continue
        ok = "never" if run.last_ok_finished is None else f"{_age(now - run.last_ok_finished)} ago"
        lines.append(f"{kind:<7} last run: {run.last_status}   last good run: {ok}")
    lines.append("")
    if alarms:
        lines.append(f"{len(alarms)} ALARM(S):")
        lines.extend(f"  [{alarm.code}] {alarm.message}" for alarm in alarms)
    else:
        lines.append("All good: no alarms.")
    return "\n".join(lines)


# ---- entry point


async def check(
    session: AsyncSession, settings: Settings | None = None
) -> tuple[Health, list[Alarm]]:
    """Collect, evaluate and log every alarm. Returns what the report needs."""
    health = await collect(session)
    alarms = evaluate(health, datetime.now(UTC), intervals(settings or get_settings()))
    for alarm in alarms:
        log.error("sync_alarm", extra={"code": alarm.code, "detail": alarm.message})
    return health, alarms


async def main(*, list_failed: bool, retry_failed: bool, event_id: int | None) -> int:
    setup_logging(get_settings().log_level)
    sessionmaker = get_sessionmaker()
    try:
        if retry_failed:
            async with sessionmaker() as session, session.begin():
                count = await repo.requeue_failed_events(session, event_id)
            print(f"{count} dead-letter event(s) put back to 'pending'.")
            if count:
                print("The sync worker will take them now (uv run python -m app.sync.worker).")
            return 0
        if list_failed:
            async with sessionmaker() as session:
                rows = await repo.list_failed_events(session)
            if not rows:
                print("The dead letter is empty.")
            for row in rows:
                print(
                    f"#{row.id}  {row.topic}  {row.resource_id}  tries={row.attempts}  "
                    f"error={row.error}"
                )
            return 0
        async with sessionmaker() as session:
            health, alarms = await check(session)
        print(format_report(health, alarms, datetime.now(UTC)))
        return 1 if alarms else 0
    except AppError as exc:
        print(f"ERROR: {exc.message}")
        return 1
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Is the Shopify sync healthy?")
    parser.add_argument("--list-failed", action="store_true", help="show the dead letter")
    parser.add_argument("--retry-failed", action="store_true", help="put dead-letter events back")
    parser.add_argument("--event-id", type=int, help="with --retry-failed: only this event")
    args = parser.parse_args()
    if args.list_failed and args.retry_failed:
        parser.error("give --list-failed or --retry-failed, not both")
    if args.event_id is not None and not args.retry_failed:
        parser.error("--event-id only works with --retry-failed")
    raise SystemExit(
        asyncio.run(
            main(
                list_failed=args.list_failed,
                retry_failed=args.retry_failed,
                event_id=args.event_id,
            )
        )
    )
