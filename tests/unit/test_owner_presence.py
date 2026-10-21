"""Bot ON / OFF button for the team: the pure rules (no database, no network)."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from app.core.settings import Settings
from app.domain.models import ConversationState
from app.integrations.whatsapp.payloads import parse_webhook
from app.owner.presence import hold_cutoff, local_time, team_answered

NOON_PK = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)  # 12:00 in Pakistan (+05:00)


def settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def conv() -> ConversationState:
    return ConversationState(conversation_id="1", sender_hash="h")


def test_bot_on_nothing_waits() -> None:
    assert hold_cutoff(settings(), NOON_PK, bot_off=False) is None


def test_bot_off_a_new_message_waits_the_set_minutes() -> None:
    chosen = settings(team_wait_minutes=3)
    assert hold_cutoff(chosen, NOON_PK, bot_off=True) == NOON_PK - timedelta(minutes=3)


def test_the_default_wait_is_three_minutes_and_there_is_no_page() -> None:
    default = settings()
    assert default.team_wait_minutes == 3
    assert default.team_password.get_secret_value() == ""


def test_a_team_reply_counts_only_for_messages_before_it() -> None:
    state = conv()
    assert team_answered(state, NOON_PK) is False  # no team reply at all
    state.team_replied_at = NOON_PK
    assert team_answered(state, NOON_PK - timedelta(minutes=1)) is True  # message, then reply
    assert team_answered(state, NOON_PK) is True  # the same moment counts
    assert team_answered(state, NOON_PK + timedelta(seconds=1)) is False  # a newer message
    assert team_answered(state, None) is False


def test_local_time_is_shown_in_the_store_time_zone() -> None:
    assert local_time(NOON_PK, "+05:00") == "20 Oct 12:00"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"team_wait_minutes": 0}, "TEAM_WAIT_MINUTES"),
        ({"team_wait_minutes": 61}, "TEAM_WAIT_MINUTES"),
        ({"team_password": SecretStr("short")}, "TEAM_PASSWORD"),
    ],
)
def test_a_typo_in_the_team_settings_fails_fast(values: dict[str, object], message: str) -> None:
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
