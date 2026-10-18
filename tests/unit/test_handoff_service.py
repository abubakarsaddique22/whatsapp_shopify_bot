"""Phase 11, step 5: handoff/service.py, the parts that need no database.

The pause, the business-hours rule (including over midnight and another time zone), the
after-hours sentence, the staff summary and the settings that feed them.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.agent.templates import render
from app.core.settings import Settings
from app.domain.models import ConversationState, Lang
from app.handoff import service


def settings(**changes) -> Settings:
    return Settings(_env_file=None, **changes)  # type: ignore[call-arg]


def conv(**changes) -> ConversationState:
    return ConversationState(conversation_id="1", sender_hash="h1", **changes)


def at(hour: int, minute: int = 0) -> datetime:
    """A UTC moment on a fixed day."""
    return datetime(2026, 10, 17, hour, minute, tzinfo=UTC)


# ---- the pause


def test_the_pause_lasts_handoff_pause_hours():
    now = at(9)
    assert service.pause_until(now, settings(handoff_pause_hours=12)) == now + timedelta(hours=12)
    assert service.pause_until(now, settings(handoff_pause_hours=1)) == now + timedelta(hours=1)


def test_no_pause_means_not_paused():
    assert service.is_paused(conv()) is False


def test_a_future_pause_is_a_pause_and_a_past_one_is_not():
    now = at(9)
    assert service.is_paused(conv(bot_paused_until=now + timedelta(minutes=1)), now) is True
    assert service.is_paused(conv(bot_paused_until=now - timedelta(minutes=1)), now) is False


def test_the_pause_ends_exactly_at_bot_paused_until():
    now = at(9)
    assert service.is_paused(conv(bot_paused_until=now), now) is False


def test_a_pause_without_a_time_zone_counts_as_utc():
    naive_future = datetime(2026, 10, 17, 10, 0)  # no tzinfo
    assert service.is_paused(conv(bot_paused_until=naive_future), at(9)) is True
    assert service.is_paused(conv(bot_paused_until=naive_future), at(11)) is False


# ---- business hours (default 10:00-20:00 at +05:00)


@pytest.mark.parametrize(
    ("utc_hour", "utc_minute", "open_now"),
    [
        (4, 59, False),  # 09:59 in Lahore
        (5, 0, True),  # 10:00: the start is included
        (10, 0, True),  # 15:00
        (14, 59, True),  # 19:59
        (15, 0, False),  # 20:00: the end is not included
        (22, 0, False),  # 03:00 the next day
    ],
)
def test_default_business_hours_are_read_in_the_configured_offset(utc_hour, utc_minute, open_now):
    assert service.is_open_now(settings(), at(utc_hour, utc_minute)) is open_now


def test_business_hours_can_run_over_midnight():
    night = settings(business_hours="20:00-02:00")  # +05:00
    assert service.is_open_now(night, at(18, 0)) is True  # 23:00
    assert service.is_open_now(night, at(20, 30)) is True  # 01:30
    assert service.is_open_now(night, at(21, 0)) is False  # 02:00
    assert service.is_open_now(night, at(5, 0)) is False  # 10:00


def test_the_same_start_and_end_means_open_all_day():
    always = settings(business_hours="09:00-09:00")
    assert service.is_open_now(always, at(0)) is True
    assert service.is_open_now(always, at(12)) is True


def test_a_negative_offset_moves_the_hours_the_other_way():
    shop = settings(business_hours="09:00-17:00", business_utc_offset="-05:00")
    assert service.is_open_now(shop, at(15)) is True  # 10:00 local
    assert service.is_open_now(shop, at(23)) is False  # 18:00 local


def test_an_offset_with_minutes_is_honoured():
    shop = settings(business_hours="10:00-20:00", business_utc_offset="+05:30")
    assert service.is_open_now(shop, at(4, 29)) is False  # 09:59
    assert service.is_open_now(shop, at(4, 30)) is True  # 10:00


def test_a_moment_without_a_time_zone_counts_as_utc():
    assert service.is_open_now(settings(), datetime(2026, 10, 17, 10, 0)) is True  # 15:00 local


# ---- the after-hours sentence


def test_no_sentence_while_the_team_is_open():
    assert service.after_hours_note("en", settings(), at(10)) is None


@pytest.mark.parametrize("lang", ["en", "roman_ur", "en_roman_ur", "mixed", "ur"])
def test_the_sentence_names_the_hours_in_every_reply_language(lang):
    note = service.after_hours_note(lang, settings(business_hours="10:00-20:00"), at(22))
    assert note is not None
    assert "10:00-20:00" in note


def test_the_sentence_is_the_template_for_that_language():
    note = service.after_hours_note("roman_ur", settings(), at(22))
    assert note == render("after_hours_note", "roman_ur", hours="10:00-20:00")


# ---- urgency and the staff summary


def test_a_complaint_is_urgent_and_the_rest_is_normal():
    assert service.urgency_for("complaint") == "high"
    for reason in ("human_request", "unclear", "composer_failed", "repeated_clarification"):
        assert service.urgency_for(reason) == "normal"


def history(*customer_lines: str) -> list[dict[str, str]]:
    lines: list[dict[str, str]] = []
    for text in customer_lines:
        lines += [{"role": "customer", "text": text}, {"role": "bot", "text": "reply"}]
    return lines


def test_the_summary_has_reason_language_product_and_what_was_said():
    state = conv(language=Lang.ROMAN_UR, history=history("watch ka rate kya hai?"))

    summary = service.build_summary(state, "human_request", product_title="Qorvex Reef")

    assert summary.splitlines() == [
        "Reason: human_request",
        "Language: roman_ur",
        "Product: Qorvex Reef",
        "Customer said: watch ka rate kya hai?",
    ]


def test_the_summary_says_when_no_product_was_discussed_and_no_one_spoke():
    summary = service.build_summary(conv(), "unclear")
    assert "Product: none discussed" in summary
    assert "Customer said" not in summary


def test_the_summary_keeps_only_the_last_customer_lines_and_never_the_bot():
    state = conv(history=history("one", "two", "three", "four"))
    summary = service.build_summary(state, "complaint")
    assert "Customer said: two | three | four" in summary
    assert "one" not in summary.replace("none discussed", "")
    assert "reply" not in summary  # bot lines are not forwarded


def test_a_long_customer_line_is_clipped_to_one_short_line():
    state = conv(history=history("word " * 200))
    line = next(x for x in service.build_summary(state, "x").splitlines() if x.startswith("Cust"))
    assert len(line) <= len("Customer said: ") + service.SUMMARY_LINE_CHARS
    assert line.endswith("...")


def test_a_card_number_in_the_history_is_masked_in_the_summary():
    state = conv(history=history("my card is 4111 1111 1111 1111 please"))
    summary = service.build_summary(state, "private_data_request")
    assert "4111" not in summary
    assert "[CARD]" in summary


def test_a_note_is_added_as_the_last_line_and_the_summary_stays_short():
    state = conv(history=history("hello"))
    summary = service.build_summary(state, "complaint", product_title="X", note="  wants a call ")
    lines = summary.splitlines()
    assert lines[-1] == "Note: wants a call"
    assert len(lines) <= 5


# ---- the new settings


@pytest.mark.parametrize("bad", ["5", "+5:00", "05:00", "+25:00", "+05:60", "UTC+5"])
def test_a_bad_utc_offset_stops_the_start_up(bad):
    with pytest.raises(ValueError, match="BUSINESS_UTC_OFFSET"):
        settings(business_utc_offset=bad)


@pytest.mark.parametrize("good", ["+05:00", "-05:00", "+00:00", "+05:30", "+14:00"])
def test_a_good_utc_offset_is_accepted(good):
    assert settings(business_utc_offset=good).business_utc_offset == good
