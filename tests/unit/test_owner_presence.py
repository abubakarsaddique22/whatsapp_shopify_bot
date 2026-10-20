"""P8 owner first, bot as backup: the pure rules (no database, no network)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.settings import Settings
from app.domain.models import ConversationState
from app.integrations.whatsapp.payloads import parse_webhook
from app.owner.presence import (
    extend_owner_pause,
    hold_cutoff,
    in_hours,
    owner_available,
    owner_hours,
    owner_is_handling,
)

# 12:00 in Pakistan (+05:00) = 07:00 UTC
NOON_PK = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
NIGHT_PK = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)  # 23:00 in Pakistan


def settings(**values: object) -> Settings:
    base: dict[str, object] = {"owner_first": True, "business_hours": "10:00-20:00"}
    return Settings(_env_file=None, **{**base, **values})  # type: ignore[arg-type]


def conv() -> ConversationState:
    return ConversationState(conversation_id="1", sender_hash="h")


@pytest.mark.parametrize(
    ("hours", "moment", "expected"),
    [
        ("10:00-20:00", NOON_PK, True),
        ("10:00-20:00", NIGHT_PK, False),
        ("22:00-02:00", NIGHT_PK, True),  # over midnight
        ("00:00-00:00", NIGHT_PK, True),  # the same start and end = all day
        ("12:00-13:00", NOON_PK, True),  # the start is included
        ("09:00-12:00", NOON_PK, False),  # the end is not
    ],
)
def test_in_hours(hours: str, moment: datetime, expected: bool) -> None:
    assert in_hours(hours, "+05:00", moment) is expected


def test_owner_hours_fall_back_to_business_hours() -> None:
    assert owner_hours(settings()) == "10:00-20:00"
    assert owner_hours(settings(owner_hours="12:00-18:00")) == "12:00-18:00"


def test_off_by_default_nothing_waits() -> None:
    off = Settings(_env_file=None)
    assert off.owner_first is False
    assert hold_cutoff(off, NOON_PK) is None


def test_in_owner_hours_a_new_message_waits_the_set_minutes() -> None:
    assert hold_cutoff(settings(owner_wait_minutes=3), NOON_PK) == NOON_PK - timedelta(minutes=3)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"now": NIGHT_PK}, id="outside-owner-hours"),
        pytest.param({"away": True}, id="owner-away"),
        pytest.param({"owner_wait_minutes": 0}, id="no-wait"),
    ],
)
def test_the_bot_answers_at_once_when(case: dict[str, object]) -> None:
    now = case.pop("now", NOON_PK)
    away = bool(case.pop("away", False))
    chosen = settings(**case)
    assert hold_cutoff(chosen, now, away=away) is None  # type: ignore[arg-type]


def test_owner_available_needs_the_setting_hours_and_presence() -> None:
    assert owner_available(settings(), NOON_PK) is True
    assert owner_available(settings(owner_first=False), NOON_PK) is False
    assert owner_available(settings(), NOON_PK, away=True) is False
    assert owner_available(settings(owner_hours="13:00-14:00"), NOON_PK) is False


def test_an_owner_reply_silences_the_bot_for_the_pause() -> None:
    state = conv()
    assert owner_is_handling(state, NOON_PK) is False
    extend_owner_pause(state, NOON_PK, settings(owner_reply_pause_minutes=60))
    assert owner_is_handling(state, NOON_PK + timedelta(minutes=59)) is True
    assert owner_is_handling(state, NOON_PK + timedelta(minutes=60)) is False


def test_a_later_owner_reply_extends_the_pause_and_an_earlier_one_never_shortens_it() -> None:
    state = conv()
    chosen = settings(owner_reply_pause_minutes=60)
    extend_owner_pause(state, NOON_PK + timedelta(minutes=30), chosen)
    extend_owner_pause(state, NOON_PK, chosen)  # an old echo arriving late
    assert state.owner_active_until == NOON_PK + timedelta(minutes=90)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"owner_hours": "10-20"}, "OWNER_HOURS"),
        ({"owner_wait_minutes": 61}, "OWNER_WAIT_MINUTES"),
        ({"owner_wait_minutes": -1}, "OWNER_WAIT_MINUTES"),
        ({"owner_reply_pause_minutes": 0}, "OWNER_REPLY_PAUSE_MINUTES"),
    ],
)
def test_a_typo_in_the_owner_settings_fails_fast(values: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_an_owner_echo_is_parsed_from_the_webhook() -> None:
    body = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "smb_message_echoes",
                        "value": {
                            "messaging_product": "whatsapp",
                            "message_echoes": [
                                {
                                    "from": "923000000000",
                                    "to": "923001234567",
                                    "id": "wamid.echo-1",
                                    "timestamp": "1760169600",
                                    "type": "text",
                                    "text": {"body": "Ji, main dekhta hoon"},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }
    parsed = parse_webhook(body)
    assert parsed.messages == []  # an owner message is never treated as a customer message
    assert len(parsed.owner_echoes) == 1
    echo = parsed.owner_echoes[0]
    assert echo.wamid == "wamid.echo-1"
    assert echo.to_wa_id == "923001234567"
    assert echo.text == "Ji, main dekhta hoon"
    assert echo.wa_timestamp == datetime(2025, 10, 11, 8, 0, tzinfo=UTC)
