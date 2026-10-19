"""The automatic part of docs/security_checklist.md: safe defaults and no secret in Git.

No database needed. A failure here means a setting or a file went back to an unsafe state.
"""

import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import dev
from app.core.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
SECRET_NAME = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|PEPPER)$")
HARMLESS = {"", "ollama"}  # empty, or a default that is not a secret


def env_example() -> dict[str, str]:
    values = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


# ---- Git


def test_the_env_file_is_ignored_by_git_and_the_example_is_not() -> None:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in lines and ".env.*" in lines and "!.env.example" in lines


def test_audio_files_are_ignored_by_git() -> None:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/audio/" in lines and "*.ogg" in lines


@pytest.mark.parametrize("name", [n for n in env_example() if SECRET_NAME.search(n)])
def test_the_example_env_holds_no_real_secret(name: str) -> None:
    value = env_example()[name]
    assert value in HARMLESS or value.startswith("change-me"), f"{name} looks like a real secret"


def test_the_example_env_holds_no_slack_url_or_token_shape() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "hooks.slack.com" not in text
    assert not re.search(r"\b(?:EAA|gsk_|sk-|shpat_|shpss_)[A-Za-z0-9]{8,}", text)


# ---- unsafe features are off by default


def test_the_example_env_starts_safe() -> None:
    values = env_example()
    assert values["KILL_SWITCH"] == "false"
    assert values["ADMIN_ENDPOINTS_ENABLED"] == "false"
    assert values["DEBUG_REPLY"] == "false"
    assert values["LANGSMITH_TRACING"] == "false"
    assert values["APP_ENV"] == "local"


def test_the_code_defaults_are_safe() -> None:
    settings = Settings(_env_file=None)
    assert settings.admin_endpoints_enabled is False
    assert settings.debug_reply is False
    assert settings.langsmith_tracing is False
    assert settings.kill_switch is False
    assert settings.show_exact_stock is False
    assert settings.urdu_script_replies is False


def test_the_default_rate_limits_are_ordered() -> None:
    settings = Settings(_env_file=None)
    assert 1 <= settings.rate_limit_per_minute <= settings.rate_limit_per_hour
    assert settings.rate_limit_per_hour <= settings.rate_limit_per_day


# ---- production refuses to start unsafe


def test_production_refuses_to_start_without_its_secrets() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")


def test_production_refuses_the_placeholder_secrets() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            app_env="production",
            shopify_shop_domain="shop.example.com",
            whatsapp_app_secret="a-real-looking-secret",
            whatsapp_access_token="EAAGrealtokenrealtokenrealtoken1",
            whatsapp_verify_token="change-me-random",
            phone_hash_pepper="change-me-random",
        )


def test_the_admin_endpoints_need_a_strong_key() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_endpoints_enabled=True)  # no key
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_endpoints_enabled=True, admin_api_key="short")
    with pytest.raises(ValidationError):
        weak = "change-me-" + "x" * 30
        Settings(_env_file=None, admin_endpoints_enabled=True, admin_api_key=weak)
    Settings(_env_file=None, admin_endpoints_enabled=True, admin_api_key="k" * 32)  # fine


def test_a_log_only_handoff_channel_is_refused_in_production() -> None:
    from app.core.errors import ConfigError
    from app.handoff.notifier import Notification, _send_log

    settings = Settings.model_construct(app_env="production", handoff_channel="log")
    with pytest.raises(ConfigError):
        _send_log(Notification("subject", "body"), settings)


def test_the_dev_endpoints_do_not_exist_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dev, "get_settings", lambda: Settings(_env_file=None, app_env="staging"))
    with pytest.raises(HTTPException) as caught:
        dev._require_local()
    assert caught.value.status_code == 404
