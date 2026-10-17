"""Unit tests for the sync monitor checks (Phase 10, step 7). No database, no network, no clock."""

from datetime import UTC, datetime, timedelta

from app.core.settings import Settings
from app.sync import monitor
from app.sync.monitor import Health, RunInfo

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
EVERY = {
    "delta": timedelta(minutes=5),
    "full": timedelta(hours=24),
    "policy": timedelta(minutes=60),
}


def good_run(minutes_ago: float = 1) -> RunInfo:
    finished = NOW - timedelta(minutes=minutes_ago)
    return RunInfo("ok", finished, finished, None)


def healthy() -> Health:
    return Health(
        event_counts={"done": 10, "ignored": 2},
        oldest_due=None,
        runs={"delta": good_run(), "full": good_run(60), "policy": good_run(30)},
    )


def codes(health: Health) -> list[str]:
    return [alarm.code for alarm in monitor.evaluate(health, NOW, EVERY)]


def test_a_healthy_sync_has_no_alarms() -> None:
    assert codes(healthy()) == []


def test_one_dead_letter_event_is_an_alarm() -> None:
    health = healthy()
    health.event_counts["failed"] = 1
    assert codes(health) == ["dead_letter"]


def test_an_event_that_waited_a_few_seconds_is_fine() -> None:
    health = healthy()
    health.oldest_due = NOW - timedelta(seconds=20)
    assert codes(health) == []


def test_an_event_that_waited_too_long_means_the_worker_is_down() -> None:
    health = healthy()
    health.oldest_due = NOW - timedelta(minutes=monitor.BACKLOG_MINUTES + 1)
    assert codes(health) == ["backlog"]


def test_a_job_that_never_ran_is_an_alarm() -> None:
    health = healthy()
    del health.runs["full"]
    assert codes(health) == ["no_good_run"]


def test_a_failed_last_run_is_an_alarm_even_if_an_older_run_was_good() -> None:
    health = healthy()
    health.runs["delta"] = RunInfo("failed", NOW, NOW - timedelta(minutes=2), "Shopify is down")
    assert codes(health) == ["run_failed"]


def test_a_run_that_says_running_for_hours_is_stuck() -> None:
    health = healthy()
    health.runs["full"] = RunInfo(
        "running", NOW - timedelta(hours=3), NOW - timedelta(hours=5), None
    )
    assert codes(health) == ["run_stuck"]


def test_a_run_that_started_a_minute_ago_is_not_stuck() -> None:
    health = healthy()
    health.runs["full"] = RunInfo(
        "running", NOW - timedelta(minutes=1), NOW - timedelta(hours=5), None
    )
    assert codes(health) == []


def test_a_delta_job_that_stopped_running_goes_stale() -> None:
    health = healthy()
    health.runs["delta"] = good_run(minutes_ago=5 * 3 + 1)  # 3 intervals plus one minute
    assert codes(health) == ["run_stale"]


def test_a_delta_job_one_interval_late_is_still_fine() -> None:
    health = healthy()
    health.runs["delta"] = good_run(minutes_ago=7)
    assert codes(health) == []


def test_the_nightly_job_is_stale_after_a_day_and_a_half() -> None:
    health = healthy()
    health.runs["full"] = good_run(minutes_ago=36 * 60 + 1)
    assert codes(health) == ["run_stale"]


def test_a_job_with_only_a_running_row_has_no_good_run_yet() -> None:
    health = healthy()
    health.runs["policy"] = RunInfo("running", NOW - timedelta(minutes=1), None, None)
    assert codes(health) == ["no_good_run"]


def test_several_problems_give_several_alarms() -> None:
    health = healthy()
    health.event_counts["failed"] = 3
    health.oldest_due = NOW - timedelta(hours=1)
    assert codes(health) == ["dead_letter", "backlog"]


def test_age_is_written_for_people() -> None:
    assert monitor._age(timedelta(seconds=45)) == "45 s"
    assert monitor._age(timedelta(minutes=12)) == "12 min"
    assert monitor._age(timedelta(hours=3, minutes=5)) == "3 h 5 min"
    assert monitor._age(timedelta(hours=24)) == "1 d"
    assert monitor._age(timedelta(hours=52)) == "2 d 4 h"


def test_intervals_follow_the_settings_with_a_minimum_of_one() -> None:
    settings = Settings(
        reconcile_delta_minutes=0, reconcile_policy_minutes=30, reconcile_full_hours=12
    )
    every = monitor.intervals(settings)
    assert every["delta"] == timedelta(minutes=1)
    assert every["policy"] == timedelta(minutes=30)
    assert every["full"] == timedelta(hours=12)


def test_the_report_names_every_alarm() -> None:
    health = healthy()
    health.event_counts["failed"] = 2
    alarms = monitor.evaluate(health, NOW, EVERY)
    report = monitor.format_report(health, alarms, NOW)
    assert "1 ALARM(S)" in report
    assert "[dead_letter]" in report
    assert "failed 2" in report


def test_the_report_says_all_good_when_nothing_is_wrong() -> None:
    report = monitor.format_report(healthy(), [], NOW)
    assert "All good" in report
