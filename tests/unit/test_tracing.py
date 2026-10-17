"""Phase 9: configure_tracing() copies the LangSmith settings into the process environment.

pydantic-settings reads `.env` into the Settings object only, but LangSmith reads os.environ, so
this small function is what really switches tracing on.
"""

import os

import pytest

from app.core.settings import Settings
from app.core.tracing import configure_tracing

NAMES = ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start without the variables, and let monkeypatch remove whatever the function sets."""
    for name in NAMES:
        monkeypatch.setenv(name, "placeholder")  # makes monkeypatch undo the change afterwards
        monkeypatch.delenv(name)


def test_tracing_stays_off_by_default() -> None:
    assert configure_tracing(Settings(_env_file=None)) is False  # type: ignore[call-arg]
    assert "LANGSMITH_TRACING" not in os.environ


def test_tracing_without_a_key_stays_off() -> None:
    settings = Settings(_env_file=None, langsmith_tracing=True)  # type: ignore[call-arg]
    assert configure_tracing(settings) is False
    assert "LANGSMITH_TRACING" not in os.environ


def test_tracing_with_a_key_exports_the_three_variables() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        langsmith_tracing=True,
        langsmith_api_key="lsv2-test-key-123",
        langsmith_project="wa-shopify-bot-test",
    )

    assert configure_tracing(settings) is True

    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2-test-key-123"
    assert os.environ["LANGSMITH_PROJECT"] == "wa-shopify-bot-test"
