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
