"""Unit tests for the pure parts of reconciliation (Phase 10, step 6). No database, no network."""

from datetime import UTC, datetime, timedelta, timezone

from app.integrations.shopify.gateway import shopify_timestamp
from app.sync import reconcile

START = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


def test_the_first_delta_run_looks_one_day_back() -> None:
    assert reconcile.delta_since(None, START) == START - timedelta(days=1)


def test_a_later_delta_run_starts_a_little_before_the_last_watermark() -> None:
    watermark = START - timedelta(minutes=5)
    assert reconcile.delta_since(watermark, START) == watermark - reconcile.OVERLAP


def test_the_overlap_is_not_zero() -> None:
    """A product edited while the previous run was working must not fall into a gap."""
    assert reconcile.OVERLAP > timedelta(0)


def test_the_three_jobs_exist_and_only_delta_has_a_watermark() -> None:
    assert set(reconcile.JOBS) == {"delta", "full", "policy"}
    assert reconcile.WATERMARK_KINDS == {"delta"}


def test_a_good_run_waits_a_whole_interval() -> None:
    assert reconcile.wait_before_next("ok", 86400.0) == 86400.0


def test_a_failed_nightly_run_is_tried_again_soon_not_after_a_day() -> None:
    assert reconcile.wait_before_next("failed", 86400.0) == reconcile.RETRY_AFTER_ERROR_SECONDS


def test_a_failed_run_never_waits_longer_than_its_own_interval() -> None:
    assert reconcile.wait_before_next("failed", 30.0) == 30.0


def test_shopify_timestamp_is_utc_without_fractions() -> None:
    karachi = timezone(timedelta(hours=5))
    moment = datetime(2026, 10, 17, 17, 30, 5, 987654, tzinfo=karachi)
    assert shopify_timestamp(moment) == "2026-10-17T12:30:05Z"
