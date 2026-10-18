import pytest
from pydantic import ValidationError

from app.core.settings import Settings


def test_defaults_load_without_env():
    s = Settings(_env_file=None)
    assert s.shopify_api_version == "2026-10"
    assert s.app_env == "local"


def test_secret_never_prints():
    s = Settings(_env_file=None, whatsapp_app_secret="super-secret-value")
    assert "super-secret-value" not in repr(s)
    assert "super-secret-value" not in str(s.whatsapp_app_secret)


def test_production_requires_secrets():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")


# ---- Phase 11 settings ----


def test_phase11_defaults():
    s = Settings(_env_file=None)
    assert s.daily_cost_budget == 0.0  # 0 = budget check off
    assert s.handoff_channel == "email"
    assert s.admin_endpoints_enabled is False
    assert s.kill_switch is False


@pytest.mark.parametrize("channel", ["email", "slack", "whatsapp_staff", "log"])
def test_handoff_channel_accepts_known_values(channel):
    assert Settings(_env_file=None, handoff_channel=channel).handoff_channel == channel


def test_handoff_channel_rejects_a_typo():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, handoff_channel="emial")


@pytest.mark.parametrize("hours", ["10-20", "25:00-26:00", "10:00", "", "10:00-20:60"])
def test_business_hours_rejects_bad_format(hours):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, business_hours=hours)


def test_business_hours_accepts_good_format():
    assert Settings(_env_file=None, business_hours="09:30-18:00").business_hours == "09:30-18:00"


def test_negative_budget_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, daily_cost_budget=-1)


@pytest.mark.parametrize("name", ["cost_per_text_message", "cost_per_voice_message"])
def test_a_negative_message_cost_is_rejected(name):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: -0.5})


def test_message_costs_default_to_free():
    s = Settings(_env_file=None)
    assert (s.cost_per_text_message, s.cost_per_voice_message) == (0.0, 0.0)


RATE_LIMIT_NAMES = ["rate_limit_per_minute", "rate_limit_per_hour", "rate_limit_per_day"]


@pytest.mark.parametrize("name", RATE_LIMIT_NAMES)
def test_rate_limits_must_be_positive(name):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: 0})


def test_pause_hours_must_be_positive():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, handoff_pause_hours=0)


def test_admin_endpoints_need_a_strong_key():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_endpoints_enabled=True)  # empty key
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_endpoints_enabled=True, admin_api_key="short")
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            admin_endpoints_enabled=True,
            admin_api_key="change-me-long-random-key-here",
        )


def test_admin_endpoints_open_with_a_strong_key():
    s = Settings(
        _env_file=None, admin_endpoints_enabled=True, admin_api_key="x7Kq9mT2vB4nR8wZ1cL5dF3h"
    )
    assert s.admin_endpoints_enabled is True


def test_weak_admin_key_is_fine_while_endpoints_are_off():
    assert Settings(_env_file=None, admin_api_key="short").admin_endpoints_enabled is False
